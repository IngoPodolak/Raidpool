"""Duplizierung herstellen – wie StableBit DrivePool, aber mit normalen Dateien auf jeder Platte.

* Jede Datei liegt als ganz normale Datei im PoolPart-Ordner einer oder mehrerer Platten.
* Soll pro Ordner (vererbt) -> fehlende Kopien werden auf die Platte(n) mit dem meisten freien
  Platz kopiert, überzählige entfernt.
* Echtzeit: Änderungen an den Platten (über mergerfs/SMB) werden per inotify erkannt; neue Dateien
  werden nach kurzer Ruhezeit dupliziert, geänderte Kopien auf die anderen Platten nachgezogen.
* Abgleich: regelmäßig vollständiger Abgleich anhand des Datei-Index (fängt Verpasstes ab).
* Sicherheit: Kopien entstehen als Temp-Datei, werden geprüft und erst dann umbenannt.
  Überzählige Kopien werden nur gelöscht, wenn alle Kopien gleich groß und gleich alt sind.
"""
import collections
import ctypes
import datetime as dt
import errno
import stat
import logging
import os
import select
import struct
import threading
import time
import uuid as uuidlib

from . import config
from .dupes import lower_priority
from .index import SKIP_NAMES, effective_target, parent_of

log = logging.getLogger("nas")

CHUNK = 4 << 20
TMP_PREFIX = ".nas-tmp-"
IGNORE_PARTS = {".nas-trash", ".nas-recycle", ".covefs", "$RECYCLE.BIN", "System Volume Information"}

DEFAULTS = {
    "enabled": True,
    "realtime": True,
    "delay_seconds": 30,          # Ruhezeit nach dem letzten Schreiben, bevor dupliziert wird
    "remove_excess": False,       # überzählige Kopien entfernen (z. B. nach Senken des Solls) – bewusst aus
    "conflict": "newest",         # newest = neueste Kopie gewinnt, manual = nur melden
    "min_free_gb": 20,            # so viel bleibt auf jeder Platte mindestens frei
    "max_mb_s": 0,                # 0 = unbegrenzt
    "reconcile_time": "04:00",    # täglicher vollständiger Abgleich
    "balance_enabled": False,
    "balance_threshold_pct": 10,  # ab diesem Unterschied der Füllstände wird ausbalanciert
    "balance_time": "05:00",
    "balance_max_gb": 500,        # höchstens so viel pro Lauf verschieben
}


# ---------------------------------------------------------------- inotify (ctypes, ohne Zusatzpakete)
IN_CLOSE_WRITE, IN_MOVED_FROM, IN_MOVED_TO = 0x8, 0x40, 0x80
IN_CREATE, IN_DELETE, IN_DELETE_SELF = 0x100, 0x200, 0x400
IN_Q_OVERFLOW, IN_IGNORED, IN_ISDIR = 0x4000, 0x8000, 0x40000000
WATCH_MASK = IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_DELETE_SELF


class Inotify:
    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.fd = self.libc.inotify_init1(os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self.wds = {}

    def add(self, path, info):
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), WATCH_MASK)
        if wd < 0:
            raise OSError(ctypes.get_errno(), f"inotify_add_watch {path}")
        self.wds[wd] = info
        return wd

    def read(self):
        buf = os.read(self.fd, 256 * 1024)
        i = 0
        while i < len(buf):
            wd, mask, cookie, ln = struct.unpack_from("iIII", buf, i)
            name = buf[i + 16:i + 16 + ln].rstrip(b"\0").decode("utf-8", "surrogateescape")
            i += 16 + ln
            yield wd, mask, name

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


# ---------------------------------------------------------------- fanotify (ganze Platte mit einer Markierung)
FAN_CLOSE_WRITE, FAN_CLASS_NOTIF, FAN_CLOEXEC, FAN_NONBLOCK = 0x08, 0x0, 0x1, 0x2
FAN_MARK_ADD, FAN_MARK_MOUNT, FAN_Q_OVERFLOW = 0x1, 0x10, 0x4000
AT_FDCWD = -100
_META = struct.Struct("IBBHQii")


class Fanotify:
    """Meldet jede fertig geschriebene Datei einer ganzen Platte – unabhängig von der Ordnerzahl
    und ohne das inotify-Kontingent zu belasten (braucht CAP_SYS_ADMIN)."""

    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.fanotify_mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int,
                                            ctypes.c_char_p]
        self.fd = self.libc.fanotify_init(FAN_CLASS_NOTIF | FAN_CLOEXEC | FAN_NONBLOCK, os.O_RDONLY | os.O_LARGEFILE)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "fanotify_init")
        self.marked = set()

    def mark_mount(self, path):
        if self.libc.fanotify_mark(self.fd, FAN_MARK_ADD | FAN_MARK_MOUNT, FAN_CLOSE_WRITE, AT_FDCWD,
                                   os.fsencode(path)) < 0:
            raise OSError(ctypes.get_errno(), f"fanotify_mark {path}")
        self.marked.add(path)

    def read(self):
        """Liefert (pfad, überlauf) je Ereignis."""
        try:
            buf = os.read(self.fd, 64 * 1024)
        except BlockingIOError:
            return
        i = 0
        while i + _META.size <= len(buf):
            ev_len, _v, _r, _ml, mask, fd, _pid = _META.unpack_from(buf, i)
            if ev_len < _META.size:
                break
            i += ev_len
            path = None
            if fd >= 0:
                try:
                    path = os.readlink(f"/proc/self/fd/{fd}")
                except OSError:
                    pass
                finally:
                    os.close(fd)
            yield path, bool(mask & FAN_Q_OVERFLOW)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def _ignored(rel):
    parts = rel.split("/")
    return any(p in IGNORE_PARTS or p.startswith(TMP_PREFIX) for p in parts) or (parts and parts[0] in SKIP_NAMES)


# ---------------------------------------------------------------- Replikation
class Replicator:
    def __init__(self, options, settings, pools, index):
        self.options = options
        self.settings = settings
        self.pools = pools
        self.index = index
        self.lock = threading.RLock()
        self.queue = {}                 # (pool, rel) -> [fällig_ab, master_uuid]
        self.self_ops = {}              # absoluter Pfad -> ignorieren bis
        self.activity = collections.deque(maxlen=200)
        self.stop = threading.Event()
        self.cancel = threading.Event()
        self.job = None                 # laufender Auftrag (Abgleich, Ausbalancieren, Entfernen)
        self.current = None             # aktuell kopierte Datei
        self.stats = {"copied_files": 0, "copied_bytes": 0, "removed": 0, "conflicts": 0, "errors": 0}
        self.watch = {"active": False, "dirs": 0, "error": None, "files": False}
        self.dirty_pools = set()
        self.ino = None
        self.copy_active = lambda: False
        self.mover_active = lambda: False
        self.audit = None
        self.fan = None
        self.fan_branches = []
        self.watch_budget = 0
        self.paused_reason = None       # z. B. „USV im Batteriebetrieb“

    # ------------------------------------------------------------ Einstellungen
    def cfg(self):
        c = dict(DEFAULTS)
        c.update(self.settings.data.get("replication") or {})
        return c

    def set_cfg(self, values):
        with self.settings.lock:
            cur = self.settings.data.setdefault("replication", {})
            for k, v in values.items():
                if k in DEFAULTS:
                    cur[k] = v
            self.settings.save()
        return self.cfg()

    def writable(self):
        return not self.options["read_only"]

    def log_act(self, kind, text, pool=None):
        self.activity.appendleft({"t": int(time.time()), "kind": kind, "text": text, "pool": pool})
        (log.warning if kind == "fehler" else log.info)("[%s] %s", kind, text)

    # ------------------------------------------------------------ Mitglieder
    def members(self, pool):
        """Verfügbare Mitglieder mit Pfad und Platzangaben."""
        res = []
        for uuid, branch, ok in self.pools.branches(pool):
            if not ok:
                continue
            try:
                st = os.statvfs(branch)
            except OSError:
                continue
            res.append({"uuid": uuid, "branch": branch, "free": st.f_bavail * st.f_frsize,
                        "total": st.f_blocks * st.f_frsize, "writable": self.pools.disks.is_writable(uuid)})
        return res

    def _target(self, pool, rel):
        targets = {k: int(v) for k, v in (pool.get("targets") or {}).items()}
        return effective_target(parent_of(rel), targets, int(pool.get("default_target", 1)))

    # ------------------------------------------------------------ Kopieren
    def _ignore_path(self, path, seconds=10):
        """Eigene Änderung merken: Ereignis wird ignoriert, solange die Datei genau so aussieht."""
        try:
            st = os.stat(path)
            sig = (st.st_size, st.st_mtime_ns)
        except OSError:
            sig = None
        self.self_ops[path] = (time.time() + seconds, sig)

    def _is_self_op(self, path):
        ent = self.self_ops.get(path)
        if not ent or ent[0] < time.time():
            return False
        try:
            st = os.stat(path)
        except OSError:
            return True          # eigene Löschung
        return ent[1] == (st.st_size, st.st_mtime_ns)

    def _copy(self, src, dst):
        d = os.path.dirname(dst)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f"{TMP_PREFIX}{uuidlib.uuid4().hex[:8]}-{os.path.basename(dst)}"[:250])
        st = os.stat(src)
        limit = self.cfg()["max_mb_s"] * 1e6
        done = 0
        try:
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    if self.cancel.is_set() or self.stop.is_set():
                        raise InterruptedError
                    t0 = time.monotonic()
                    data = fi.read(CHUNK)
                    if not data:
                        break
                    fo.write(data)
                    done += len(data)
                    if self.current:
                        self.current["done"] = done
                    if limit:
                        wait = len(data) / limit - (time.monotonic() - t0)
                        if wait > 0:
                            time.sleep(wait)
                fo.flush()
                os.fsync(fo.fileno())
            if os.path.getsize(tmp) != st.st_size:
                raise OSError("Größe der Kopie stimmt nicht")
            try:
                os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            except OSError:
                pass
            os.replace(tmp, dst)
            self._ignore_path(dst, 60)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        self.stats["copied_files"] += 1
        self.stats["copied_bytes"] += st.st_size
        return st.st_size

    def _remove(self, path):
        self._ignore_path(path, 30)
        os.remove(path)
        self.stats["removed"] += 1

    # ------------------------------------------------------------ Eine Datei in Ordnung bringen
    def fix_file(self, pool, rel, master=None):
        """Stellt für eine Datei Soll-Anzahl und gleiche Inhalte her. Gibt kurzen Status zurück."""
        if not self.writable():
            return "schreibgeschützt"
        members = self.members(pool)
        present = []
        for m in members:
            p = os.path.join(m["branch"], rel)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if os.path.isfile(p):
                present.append((m, p, st))
        pid = pool["id"]
        if not present:
            self.index.delete_file(pid, rel)
            return "gelöscht"
        cfg = self.cfg()
        target = min(self._target(pool, rel), len(members))

        # 1) Kopien angleichen (Größe oder Änderungszeit unterschiedlich)
        sizes = {st.st_size for _, _, st in present}
        mtimes = [st.st_mtime for _, _, st in present]
        differs = len(sizes) > 1 or (max(mtimes) - min(mtimes) > 2)
        if differs:
            src = next((x for x in present if x[0]["uuid"] == master), None)
            if src is None:
                if cfg["conflict"] != "newest":
                    self.stats["conflicts"] += 1
                    self.index.upsert_file(pid, rel, present[0][2].st_size, int(max(mtimes)),
                                           [m["uuid"] for m, _, _ in present], consistent=False)
                    self.log_act("konflikt", f"Kopien unterschiedlich, bitte prüfen: {rel}", pid)
                    return "konflikt"
                src = max(present, key=lambda x: x[2].st_mtime)
                others = [st.st_mtime for _, p, st in present if p != src[1]
                          and (st.st_size != src[2].st_size or abs(st.st_mtime - src[2].st_mtime) > 2)]
                if others and src[2].st_mtime - max(others) <= 2:
                    # gleich alt, aber unterschiedlicher Inhalt -> nicht raten
                    self.stats["conflicts"] += 1
                    self.index.upsert_file(pid, rel, src[2].st_size, int(src[2].st_mtime),
                                           [m["uuid"] for m, _, _ in present], consistent=False)
                    self.log_act("konflikt", f"Kopien unterschiedlich und gleich alt, bitte auswählen: {rel}", pid)
                    return "konflikt"
            for m, p, st in present:
                if not m["writable"]:
                    continue
                if p != src[1] and (st.st_size != src[2].st_size or abs(st.st_mtime - src[2].st_mtime) > 2):
                    self.current = {"pool": pid, "path": rel, "size": src[2].st_size, "done": 0, "action": "aktualisieren"}
                    self._copy(src[1], p)
                    self.log_act("aktualisiert", f"{rel} → {self._disk_label(m['uuid'])}", pid)
            present = [(m, p, os.stat(p)) for m, p, _ in present]

        master_entry = next((x for x in present if x[0]["uuid"] == master), present[0])
        size = master_entry[2].st_size

        # 2) Fehlende Kopien anlegen
        if len(present) < target:
            have = {m["uuid"] for m, _, _ in present}
            reserve = cfg["min_free_gb"] * 1e9
            cands = sorted((m for m in members if m["uuid"] not in have and m["writable"] and m["free"] - size > reserve),
                           key=lambda m: -m["free"])
            for m in cands[:target - len(present)]:
                dst = os.path.join(m["branch"], rel)
                self.current = {"pool": pid, "path": rel, "size": size, "done": 0, "action": "duplizieren",
                                "to": self._disk_label(m["uuid"])}
                self._copy(master_entry[1], dst)
                m["free"] -= size
                present.append((m, dst, os.stat(dst)))
                self.log_act("dupliziert", f"{rel} → {self._disk_label(m['uuid'])}", pid)
            if len(present) < target:
                self.log_act("fehler", f"Nicht genug Platten mit freiem Platz für {target}× : {rel}", pid)
                self.stats["errors"] += 1

        # 3) Überzählige Kopien entfernen (nur wenn alle Kopien identisch sind)
        elif len(present) > target and cfg["remove_excess"]:
            same = len({st.st_size for _, _, st in present}) == 1
            if same:
                keep_first = sorted(present, key=lambda x: (x[0]["uuid"] != master_entry[0]["uuid"], -x[0]["free"]))
                for m, p, _ in keep_first[target:]:
                    if not m["writable"]:
                        continue
                    self._remove(p)
                    self.log_act("entfernt", f"Überzählige Kopie {rel} von {self._disk_label(m['uuid'])}", pid)
                present = keep_first[:target]

        self.current = None
        self.index.upsert_file(pid, rel, size, int(present[0][2].st_mtime), [m["uuid"] for m, _, _ in present],
                               consistent=True)
        self.dirty_pools.add(pid)
        return "ok"

    def _disk_label(self, uuid):
        disk, part = self.pools.disks.partition(uuid)
        return (part or {}).get("label") or (disk or {}).get("model") or uuid[:8]

    def resolve(self, pool, rel, uuid=None):
        """Konflikt lösen: Kopie von Platte uuid (oder die neueste) auf alle anderen übertragen."""
        if not self.writable():
            raise ValueError("Schreibschutz aktiv")
        if uuid is None:
            best = None
            for m in self.members(pool):
                p = os.path.join(m["branch"], rel)
                if os.path.isfile(p):
                    mt = os.stat(p).st_mtime
                    if best is None or mt > best[1]:
                        best = (m["uuid"], mt)
            if not best:
                raise ValueError("Datei nicht gefunden")
            uuid = best[0]
        res = self.fix_file(pool, rel, master=uuid)
        self.index.aggregate(pool)
        return res

    def copies_of(self, pool, rel):
        out = []
        for m in self.members(pool):
            p = os.path.join(m["branch"], rel)
            if os.path.isfile(p):
                st = os.stat(p)
                out.append({"uuid": m["uuid"], "disk": self._disk_label(m["uuid"]), "size": st.st_size,
                            "mtime": int(st.st_mtime)})
        return out

    # ------------------------------------------------------------ Warteschlange
    def enqueue(self, pid, rel, master=None, delay=None):
        if _ignored(rel):
            return
        due = time.time() + (self.cfg()["delay_seconds"] if delay is None else delay)
        with self.lock:
            cur = self.queue.get((pid, rel))
            self.queue[(pid, rel)] = [due, master or (cur[1] if cur else None)]

    def worker_loop(self):
        lower_priority()
        last_agg = time.time()
        while not self.stop.wait(1):
            if not self.cfg()["enabled"] or not self.writable() or self.paused_reason:
                continue
            if self.mover_active():
                # Der Mover schreibt gerade parallel von der SSD auf die Platten (inkl. aller Kopien) –
                # dann keine Platte-zu-Platte-Duplizierung, die würde ihn nur ausbremsen
                continue
            now = time.time()
            with self.lock:
                due = [(k, v) for k, v in self.queue.items() if v[0] <= now]
                due.sort(key=lambda kv: kv[1][0])
                due = due[:200]
                for k, _ in due:
                    self.queue.pop(k, None)
            for (pid, rel), (_, master) in due:
                if self.stop.is_set():
                    break
                pool = self.settings.pool(pid)
                if not pool:
                    continue
                try:
                    self.fix_file(pool, rel, master)
                except InterruptedError:
                    self.enqueue(pid, rel, master, delay=60)
                    break
                except OSError as err:
                    if err.errno in (errno.EBUSY, errno.ETXTBSY):
                        self.enqueue(pid, rel, master, delay=60)
                    else:
                        self.stats["errors"] += 1
                        self.log_act("fehler", f"{rel}: {err}", pid)
                except Exception as err:  # noqa: BLE001
                    # z. B. Datenbank kurz gesperrt: später erneut versuchen – der Dienst darf nie stehen bleiben
                    log.warning("Duplizierung %s: %s – neuer Versuch in 2 Minuten", rel, err)
                    self.enqueue(pid, rel, master, delay=120)
                finally:
                    self.current = None
            # Ordnerstatistik höchstens alle 5 Minuten neu berechnen (bei großen Pools aufwendig)
            if self.dirty_pools and time.time() - last_agg > 300:
                for pid in list(self.dirty_pools):
                    self.dirty_pools.discard(pid)
                    pool = self.settings.pool(pid)
                    if pool:
                        try:
                            self.index.aggregate(pool)
                        except Exception as err:  # noqa: BLE001
                            log.warning("Ordnerstatistik %s: %s", pid, err)
                            self.dirty_pools.add(pid)
                last_agg = time.time()
            # abgelaufene Selbst-Ignorierungen aufräumen
            if len(self.self_ops) > 1000:
                t = time.time()
                self.self_ops = {k: v for k, v in self.self_ops.items() if v[0] > t}

    # ------------------------------------------------------------ Echtzeit-Überwachung
    def _add_tree(self, pid, uuid, branch, rel_dir, enqueue_files=False):
        stack = [rel_dir]
        while stack:
            rd = stack.pop()
            full = os.path.join(branch, rd) if rd else branch
            if rd and _ignored(rd):
                continue
            if self.watch["dirs"] >= self.watch_budget:
                # Nie das gesamte inotify-Kontingent belegen: Home Assistant/systemd brauchen es auch.
                # Neue/geänderte Dateien meldet fanotify trotzdem; nur Löschen/Verschieben in nicht
                # überwachten Ordnern wird erst beim regelmäßigen Abgleich erkannt.
                if not self.fan:
                    self.watch["error"] = (f"Echtzeit-Überwachung auf {self.watch_budget} Ordner begrenzt – weitere "
                                           "Änderungen werden beim regelmäßigen Abgleich erkannt")
                return
            try:
                self.ino.add(full, (pid, uuid, branch, rd))
                self.watch["dirs"] += 1
            except OSError as err:
                if err.errno == errno.ENOSPC:
                    self.watch["error"] = ("Zu viele Ordner für die Echtzeit-Überwachung – Änderungen werden "
                                           "zusätzlich beim täglichen Abgleich erkannt")
                    return
                continue
            try:
                with os.scandir(full) as it:
                    for e in it:
                        r = f"{rd}/{e.name}" if rd else e.name
                        if e.is_dir(follow_symlinks=False):
                            stack.append(r)
                        elif enqueue_files and e.is_file(follow_symlinks=False):
                            self.enqueue(pid, r, uuid)
            except OSError:
                pass

    def watch_loop(self):
        while not self.stop.is_set():
            if not (self.cfg()["enabled"] and self.cfg()["realtime"] and self.writable()):
                self.watch["active"] = False
                self.stop.wait(10)
                continue
            try:
                self.watch_budget = self._watch_budget()
                self.ino = Inotify()
                self.watch.update(active=True, dirs=0, error=None, files=False)
                self._setup_fanotify()
                for pool in list(self.settings.data["pools"]):
                    for uuid, branch, ok in self.pools.branches(pool):
                        if ok:
                            self._add_tree(pool["id"], uuid, branch, "")
                self.log_act("info", f"Echtzeit-Überwachung aktiv ({self.watch['dirs']} Ordner"
                                     + (", alle Dateiänderungen" if self.fan else "") + ")")
                gen = self.settings_generation()
                while not self.stop.is_set() and gen == self.settings_generation():
                    fds = [self.ino.fd] + ([self.fan.fd] if self.fan else [])
                    r, _, _ = select.select(fds, [], [], 5)
                    if not r:
                        continue
                    if self.ino.fd in r:
                        for wd, mask, name in self.ino.read():
                            self._event(wd, mask, name)
                    if self.fan and self.fan.fd in r:
                        for path, overflow in self.fan.read():
                            self._fan_event(path, overflow)
            except Exception as err:  # noqa: BLE001
                self.watch.update(active=False, error=str(err))
                log.exception("Echtzeit-Überwachung")
                self.stop.wait(30)
            finally:
                if self.ino:
                    self.ino.close()
                    self.ino = None
                if self.fan:
                    self.fan.close()
                    self.fan = None

    # ------------------------------------------------------------ Meldungen von Samba (audit.py)
    def _locate(self, path):
        """Absoluter Pfad -> (pool_id, rel, disk_uuid|None) oder None."""
        for pool in list(self.settings.data["pools"]):
            roots = [(self.pools.mountpoint(pool), None)]
            try:
                roots.append((self.pools.base_mountpoint(pool), None))
            except Exception:  # noqa: BLE001
                pass
            roots += [(b, u) for u, b, ok in self.pools.branches(pool) if ok]
            for root, uuid in roots:
                root = root.rstrip("/")
                if path.startswith(root + "/"):
                    rel = path[len(root) + 1:]
                    return (pool["id"], rel, uuid) if rel and not _ignored(rel) else None
        return None

    def _recent(self, what, rel, size):
        a = getattr(self, "audit", None)
        if a is not None:
            a.recent.appendleft({"t": int(time.time()), "what": what, "path": rel, "size": size})

    def _in_cache(self, pid, rel):
        cache = getattr(self.pools, "cache", None)
        pool = self.settings.pool(pid)
        if not cache or not pool or not cache.active(pool):
            return False
        from .cache import cache_dir
        return os.path.isfile(os.path.join(cache_dir(pool), rel))

    def external_event(self, op, path, new=None):
        if not (self.cfg()["enabled"] and self.cfg()["realtime"] and self.writable()):
            return
        loc = self._locate(path)
        if op == "write":
            if not loc:
                return
            try:
                st = os.stat(path)
            except OSError:
                return
            # nur wirklich geänderte Dateien (ctime lässt sich – anders als mtime – nicht zurücksetzen);
            # reines Lesen/Abspielen löst so keine Arbeit aus
            if not stat.S_ISREG(st.st_mode) or time.time() - st.st_ctime > 600:
                return
            if loc[2] is None and self._in_cache(loc[0], loc[1]):
                self._recent("auf SSD-Cache", loc[1], st.st_size)
                return     # liegt noch auf dem SSD-Cache – der Mover meldet die Datei nach dem Verschieben
            if not self._is_self_op(path):
                self.enqueue(loc[0], loc[1], loc[2])
                self._recent("geschrieben", loc[1], st.st_size)
        elif op == "delete":
            if loc:
                self._recent("gelöscht", loc[1], None)
                self.index.delete_prefix(loc[0], loc[1])
                self.enqueue(loc[0], loc[1], None)
        elif op == "rename":
            nloc = self._locate(new) if new else None
            if nloc:
                self._recent("umbenannt", nloc[1], None)
            if loc:
                self.index.delete_prefix(loc[0], loc[1])
                self.enqueue(loc[0], loc[1], None)
            if nloc:
                if os.path.isdir(new):
                    # Ordner umbenannt/verschoben: Dateien neu erfassen (mergerfs hat alle Kopien mitgenommen)
                    for root, _dirs, files in os.walk(new):
                        for f in files:
                            r = os.path.relpath(os.path.join(root, f), new)
                            self.enqueue(nloc[0], f"{nloc[1]}/{r}", nloc[2])
                else:
                    self.enqueue(nloc[0], nloc[1], nloc[2])

    def _setup_fanotify(self):
        self.fan, self.fan_branches = None, []
        try:
            fan = Fanotify()
        except OSError as err:
            log.info("fanotify nicht verfügbar (%s) – nur inotify", err)
            return
        branches = []
        for pool in list(self.settings.data["pools"]):
            for uuid, branch, ok in self.pools.branches(pool):
                if not ok or uuid.startswith("net-"):
                    continue
                try:
                    fan.mark_mount(branch)
                    branches.append((branch.rstrip("/") + "/", pool["id"], uuid))
                except OSError as err:
                    log.info("fanotify für %s nicht möglich: %s", branch, err)
        if not branches:
            fan.close()
            return
        # längste Präfixe zuerst, falls Zweige ineinander liegen
        self.fan_branches = sorted(branches, key=lambda b: -len(b[0]))
        self.fan = fan
        self.watch["files"] = True

    def _fan_event(self, path, overflow):
        if overflow:
            self.log_act("info", "Viele Änderungen auf einmal – vollständiger Abgleich wird geplant")
            try:
                self.start_job("abgleich")
            except ValueError:
                pass
            return
        if not path or path.endswith(" (deleted)"):
            return
        for prefix, pid, uuid in self.fan_branches:
            if path.startswith(prefix):
                rel = path[len(prefix):]
                if rel and not _ignored(rel) and not self._is_self_op(path):
                    self.enqueue(pid, rel, uuid)
                return

    def settings_generation(self):
        """Ändert sich, wenn Pools/Mitglieder sich ändern -> Überwachung neu aufbauen."""
        return tuple((p["id"], tuple((m["uuid"], m.get("poolpart")) for m in p["members"]))
                     for p in self.settings.data["pools"]) + (self.cfg()["realtime"], self.cfg()["enabled"])

    def _event(self, wd, mask, name):
        if mask & IN_Q_OVERFLOW:
            self.log_act("info", "Viele Änderungen auf einmal – vollständiger Abgleich wird geplant")
            self.start_job("abgleich")
            return
        info = self.ino.wds.get(wd)
        if not info or mask & IN_IGNORED:
            self.ino.wds.pop(wd, None)
            return
        pid, uuid, branch, rd = info
        rel = f"{rd}/{name}" if rd else name
        if not name or _ignored(rel):
            return
        full = os.path.join(branch, rel)
        if self._is_self_op(full):
            return
        if mask & IN_ISDIR:
            if mask & (IN_CREATE | IN_MOVED_TO):
                self._add_tree(pid, uuid, branch, rel, enqueue_files=True)
            elif mask & IN_MOVED_FROM:
                self.index.delete_prefix(pid, rel)
            return
        if mask & (IN_CLOSE_WRITE | IN_MOVED_TO):
            self.enqueue(pid, rel, uuid)
        elif mask & (IN_DELETE | IN_MOVED_FROM):
            # Löschen über den Pool entfernt alle Kopien; bleibt eine übrig, wird das Soll wiederhergestellt
            self.enqueue(pid, rel, None)

    @staticmethod
    def _watch_budget():
        """Wie viele Ordner höchstens überwacht werden. Das inotify-Kontingent teilt sich das Add-on mit
        dem Host (systemd, Netzwerkspeicher, andere Apps) – ist es aufgebraucht, schlagen dort z. B.
        Mounts fehl. Daher Limit möglichst anheben und höchstens ein Drittel davon nutzen."""
        path = "/proc/sys/fs/inotify/max_user_watches"
        cur = 8192
        try:
            with open(path, encoding="utf-8") as fh:
                cur = int(fh.read().strip())
            if cur < 1048576:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("1048576")
                with open(path, encoding="utf-8") as fh:
                    cur = int(fh.read().strip())
        except (OSError, ValueError):
            pass
        return max(1000, min(cur // 3, 400000))

    # ------------------------------------------------------------ Aufträge (Abgleich, Balance, Entfernen)
    def start_job(self, kind, **kw):
        if not self.writable():
            raise ValueError("Schreibschutz aktiv – zum Duplizieren 'read_only' in den Add-on-Optionen ausschalten")
        if self.paused_reason:
            raise ValueError(f"Pausiert: {self.paused_reason}")
        if self.job and self.job.get("running"):
            if kind == "abgleich" and self.job["kind"] == "abgleich":
                return
            raise ValueError(f"Es läuft bereits: {self.job['label']}")
        labels = {"abgleich": "Vollständiger Abgleich", "balance": "Ausbalancieren", "entfernen": "Platte entfernen"}
        self.cancel.clear()
        self.job = {"kind": kind, "label": labels[kind], "running": True, "started": int(time.time()),
                    "done": 0, "total": 0, "phase": "Vorbereiten", "kw": kw}
        target = {"abgleich": self._job_reconcile, "balance": self._job_balance, "entfernen": self._job_remove}[kind]
        threading.Thread(target=self._run_job, args=(target, kw), daemon=True, name=f"job-{kind}").start()

    def _run_job(self, fn, kw):
        lower_priority()
        try:
            fn(**kw)
            self.job["result"] = "abgebrochen" if self.cancel.is_set() else "fertig"
        except InterruptedError:
            self.job["result"] = "abgebrochen"
        except Exception as err:  # noqa: BLE001
            log.exception("Auftrag fehlgeschlagen")
            self.job["result"] = "fehler"
            self.job["error"] = str(err)
            self.log_act("fehler", f"{self.job['label']}: {err}")
        finally:
            self.job["running"] = False
            self.job["finished"] = int(time.time())
            self.current = None
            self.log_act("info", f"{self.job['label']}: {self.job['result']}")

    def _rescan(self, pool):
        t = self.pools.scan_threads.get(pool["id"])
        if t and t.is_alive():
            self.job["phase"] = f"Warte auf laufenden Scan von {pool['name']}"
            t.join()
        self.job["phase"] = f"Dateiliste {pool['name']} aktualisieren"
        branches = [(u, b) for u, b, ok in self.pools.branches(pool) if ok]
        self.index.refresh(pool, branches)   # nur geänderte Ordner; einmal pro Woche vollständig
        self.cleanup_tmp()

    def cleanup_tmp(self):
        """Reste abgebrochener Kopien (z. B. nach Neustart/Stromausfall) entfernen – nur ältere als 1 h."""
        from .index import Index
        stale, Index.stale[:] = list(Index.stale), []
        for p in stale:
            try:
                if time.time() - os.path.getmtime(p) > 3600:
                    os.remove(p)
                    self.log_act("info", f"Rest einer abgebrochenen Kopie entfernt: {os.path.basename(p)}")
            except OSError:
                pass

    def _yield_to_copies(self):
        """Hintergrund-Aufträge warten, solange über die Freigaben viel geschrieben wird – dann werden
        nur die aktuellen Dateien bearbeitet."""
        if not self.copy_active():
            return
        self.job["paused"] = "wartet – großer Kopiervorgang läuft"
        while self.copy_active() and not self.cancel.is_set() and not self.stop.is_set():
            time.sleep(5)
        self.job.pop("paused", None)

    def _job_reconcile(self, pool_id=None):
        pools = [self.settings.pool(pool_id)] if pool_id else list(self.settings.data["pools"])
        for pool in filter(None, pools):
            self._rescan(pool)
            todo = self.index.needs_fix(pool, self.cfg()["remove_excess"], len(self.members(pool)))
            self.job.update(phase=f"{pool['name']}: Soll herstellen", total=self.job["total"] + len(todo))
            for rel in todo:
                if self.cancel.is_set():
                    raise InterruptedError
                self._yield_to_copies()
                try:
                    self.fix_file(pool, rel)
                except OSError as err:
                    self.stats["errors"] += 1
                    self.log_act("fehler", f"{rel}: {err}", pool["id"])
                self.job["done"] += 1
            self.index.aggregate(pool)

    def _job_balance(self, pool_id=None):
        cfg = self.cfg()
        limit = cfg["balance_max_gb"] * 1e9
        moved = 0
        pools = [self.settings.pool(pool_id)] if pool_id else list(self.settings.data["pools"])
        for pool in filter(None, pools):
            self._rescan(pool)
            self.job["phase"] = f"{pool['name']}: ausbalancieren"
            while moved < limit and not self.cancel.is_set():
                self._yield_to_copies()
                ms = self.members(pool)
                if len(ms) < 2:
                    break
                for m in ms:
                    m["pct"] = 100 * (1 - m["free"] / m["total"]) if m["total"] else 0
                ms.sort(key=lambda m: m["pct"])
                ms = [m for m in ms if m["writable"]]
                if len(ms) < 2:
                    break
                low, high = ms[0], ms[-1]
                if high["pct"] - low["pct"] <= cfg["balance_threshold_pct"]:
                    break
                cand = self.index.balance_candidates(pool["id"], high["uuid"], low["uuid"])
                if not cand:
                    break
                progress = False
                for rel, size in cand:
                    if self.cancel.is_set() or moved >= limit:
                        break
                    src = os.path.join(high["branch"], rel)
                    dst = os.path.join(low["branch"], rel)
                    if not os.path.isfile(src) or os.path.exists(dst) or low["free"] - size < cfg["min_free_gb"] * 1e9:
                        continue
                    self.current = {"pool": pool["id"], "path": rel, "size": size, "done": 0, "action": "verschieben",
                                    "to": self._disk_label(low["uuid"])}
                    self._copy(src, dst)
                    self._remove(src)
                    self.log_act("verschoben", f"{rel}: {self._disk_label(high['uuid'])} → {self._disk_label(low['uuid'])}",
                                 pool["id"])
                    self.index.move_disk(pool["id"], rel, high["uuid"], low["uuid"])
                    moved += size
                    self.job["done"] += 1
                    progress = True
                    # nach jeweils ~5 % neu bewerten
                    if size and moved % max(high["total"] // 20, 1) < size:
                        break
                if not progress:
                    break
            self.index.aggregate(pool)
        self.job["moved_bytes"] = moved

    def _job_remove(self, pool_id, uuid):
        """Platte aus dem Pool nehmen: Dateien, die sonst fehlen oder unter Soll fallen, vorher umkopieren."""
        pool = self.settings.pool(pool_id)
        if not pool:
            raise ValueError("Pool nicht gefunden")
        if len(pool["members"]) < 2:
            raise ValueError("Die letzte Platte kann nicht entfernt werden")
        self._rescan(pool)
        others = [m for m in self.members(pool) if m["uuid"] != uuid and m["writable"]]
        src_branch = next((b for u, b, ok in self.pools.branches(pool) if u == uuid and ok), None)
        rows = self.index.files_on_disk(pool_id, uuid)
        self.job.update(phase="Dateien auf andere Platten kopieren", total=len(rows))
        cfg = self.cfg()
        missing = 0
        for rel, size, disks in rows:
            if self.cancel.is_set():
                raise InterruptedError
            remaining = [d for d in disks.split(",") if d and d != uuid]
            target = min(self._target(pool, rel), len(others))
            if len(remaining) < max(target, 1):
                need = max(target, 1) - len(remaining)
                cands = sorted((m for m in others if m["uuid"] not in remaining
                                and m["free"] - size > cfg["min_free_gb"] * 1e9), key=lambda m: -m["free"])
                if not src_branch or len(cands) < need:
                    missing += 1
                    self.log_act("fehler", f"Kein Platz für {rel}", pool_id)
                    continue
                for m in cands[:need]:
                    self.current = {"pool": pool_id, "path": rel, "size": size, "done": 0, "action": "umkopieren",
                                    "to": self._disk_label(m["uuid"])}
                    self._copy(os.path.join(src_branch, rel), os.path.join(m["branch"], rel))
                    m["free"] -= size
                    self.log_act("umkopiert", f"{rel} → {self._disk_label(m['uuid'])}", pool_id)
            self.job["done"] += 1
        if missing:
            raise RuntimeError(f"{missing} Dateien konnten nicht umkopiert werden – Platte bleibt im Pool")
        self.job["phase"] = "Platte aus dem Pool nehmen"
        with self.settings.lock:
            pool["members"] = [m for m in pool["members"] if m["uuid"] != uuid]
            self.settings.save()
        try:
            self.pools.unmount(pool)
            self.pools.mount(pool)
        except Exception as err:  # noqa: BLE001
            self.log_act("fehler", f"Pool neu einhängen: {err}", pool_id)
        self._rescan(pool)
        self.index.aggregate(pool)
        self.log_act("info", f"{self._disk_label(uuid)} aus Pool {pool['name']} entfernt – die Dateien bleiben "
                             f"auf der Platte erhalten", pool_id)

    # ------------------------------------------------------------ Planer
    def scheduler_loop(self):
        last = {}
        startup_done = False
        started = time.time()
        while not self.stop.wait(30):
            cfg = self.cfg()
            if not cfg["enabled"] or not self.writable():
                continue
            now = dt.datetime.now()
            hm = now.strftime("%H:%M")
            day = now.strftime("%Y-%m-%d")
            busy = self.job and self.job.get("running")
            try:
                if not startup_done and not busy and time.time() - started > 900:
                    # Abgleich nach dem Start (z. B. nach Stromausfall) erst nach 15 Minuten – nicht direkt
                    # nach jedem Update mitten in laufende Kopiervorgänge
                    startup_done = True
                    self.start_job("abgleich")
                elif hm == cfg["reconcile_time"] and last.get("abgleich") != day and not busy:
                    last["abgleich"] = day
                    self.start_job("abgleich")
                elif cfg["balance_enabled"] and hm == cfg["balance_time"] and last.get("balance") != day and not busy:
                    last["balance"] = day
                    self.start_job("balance")
            except ValueError:
                pass

    def _watch_view(self):
        w = dict(self.watch)
        a = getattr(self, "audit", None)
        w["smb"] = bool(a and a.active)
        if w["smb"] and w.get("error") and "begrenzt" in w["error"]:
            # Änderungen über die Freigaben meldet Samba vollständig – Ordner-Grenze betrifft nur andere Wege
            w["error"] = None
        return w

    def status(self):
        job = dict(self.job) if self.job else None
        if job:
            job.pop("kw", None)
        with self.lock:
            qlen = len(self.queue)
        return {"config": self.cfg(), "read_only": self.options["read_only"], "queue": qlen,
                "paused": self.paused_reason,
                "current": self.current, "job": job, "watch": self._watch_view(), "stats": self.stats,
                "activity": list(self.activity)[:60]}
