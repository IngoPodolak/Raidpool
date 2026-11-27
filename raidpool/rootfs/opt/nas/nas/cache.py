"""SSD-Schreib-Cache für Pools.

Aufbau mit Cache:
    Pool (SMB, …)  =  mergerfs [ Cache-Ordner (SSD) , Basis-Pool ]   – neue Dateien landen zuerst auf der SSD
    Basis-Pool     =  mergerfs [ Platte 1 , Platte 2 , … ]            – wie ohne Cache
Der Mover verschiebt Dateien aus dem Cache auf den Basis-Pool (dort verteilt mergerfs sie auf die Platten
und die Duplizierung erstellt die Kopien). Der Cache zählt nicht zum Pool-Speicher.

Cache-Ziel: die System-SSD von Home Assistant (Ordner /data/cache, nicht in HA-Backups) oder eine
eigene SSD/Partition (Ordner RAIDPoolCache/<pool>). Eine SSD kann von mehreren Pools geteilt werden.
"""
import collections
import fnmatch
import datetime as dt
import logging
import os
import threading
import time
import uuid as uuidlib

from . import config
from .dupes import lower_priority

log = logging.getLogger("nas")

SYSTEM = "system"
SYSTEM_DIR = config.DATA_DIR + "/cache"
CACHE_FOLDER = "RAIDPoolCache"
TMP = ".nas-tmp-"
DEFAULTS = {
    "enabled": False,
    "target": SYSTEM,          # "system" oder uuid einer Partition
    "max_gb": 50,              # so viel darf der Cache höchstens belegen
    "min_free_gb": 20,         # so viel bleibt auf der SSD immer frei (System-SSD: für HA!)
    "move_after_min": 30,      # Dateien, die so lange nicht geändert wurden, werden verschoben (0 = nur nach Zeit/Füllstand)
    "move_at_pct": 80,         # ab diesem Füllstand des Cache sofort verschieben
    "move_time": "03:30",      # täglich alles verschieben
    "hold_downloads": True,    # Download-/Archivteile auf der SSD halten, Ordner erst verschieben, wenn dort Ruhe ist
    "hold_hours": 24,          # Archive höchstens so lange halten (danach – oder wenn die SSD voll wird – trotzdem)
}

# Teile von Downloads und Archiven – werden meist nach dem Entpacken gelöscht, sollen also nie auf die Platten
HOLD_PATTERNS = ("*.part", "*.rar", "*.r[0-9][0-9]", "*.7z", "*.7z.[0-9][0-9][0-9]", "*.zip", "*.z[0-9][0-9]",
                 "*.[0-9][0-9][0-9]", "*.crdownload", "*.!qb", "*.tmp", "*.temp", "*.download", "*.jdc",
                 "*.part[0-9]*.rar", "*.tar", "*.gz")


def pool_cache_cfg(pool):
    c = dict(DEFAULTS)
    c.update(pool.get("cache") or {})
    return c


def cache_dir(pool):
    c = pool_cache_cfg(pool)
    if c["target"] == SYSTEM:
        return os.path.join(SYSTEM_DIR, pool["id"])
    return os.path.join(config.DISK_MNT, c["target"], CACHE_FOLDER, pool["id"])


def device_root(pool):
    c = pool_cache_cfg(pool)
    return SYSTEM_DIR if c["target"] == SYSTEM else os.path.join(config.DISK_MNT, c["target"])


class CacheManager:
    def __init__(self, options, settings, pools, disks):
        self.options = options
        self.settings = settings
        self.pools = pools
        self.disks = disks
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.on_moved = None     # Rückruf (pool_id, rel) nach dem Verschieben auf die Platten
        self.state = {}            # pool -> Status des Movers
        self.activity = collections.deque(maxlen=50)
        self.paused_reason = None
        self.flush_requests = set()

    # ------------------------------------------------------------ Einstellungen
    def set_cfg(self, pid, values):
        pool = self.settings.pool(pid)
        if not pool:
            raise ValueError("Pool nicht gefunden")
        old = pool_cache_cfg(pool)
        new = dict(old)
        for k in DEFAULTS:
            if k in values:
                new[k] = values[k]
        if new["enabled"] and self.options["read_only"]:
            raise ValueError("Schreibschutz aktiv – der SSD-Cache braucht Schreibzugriff ('read_only' ausschalten)")
        if new["target"] != SYSTEM:
            disk, part = self.disks.partition(new["target"])
            if not part or not part["mountpoint"]:
                raise ValueError("Die gewählte SSD ist nicht eingehängt")
            if any(m["uuid"] == new["target"] for p in self.settings.data["pools"] for m in p["members"]):
                raise ValueError("Diese Platte gehört zu einem Pool und kann nicht zusätzlich Cache sein")
            if not self.disks.is_writable(new["target"]):
                raise ValueError("Die gewählte SSD ist nur lesend eingebunden")
        new["max_gb"] = max(1, int(new["max_gb"]))
        new["min_free_gb"] = max(1, int(new["min_free_gb"]))
        layout_change = (old["enabled"], old["target"]) != (new["enabled"], new["target"])
        if layout_change and old["enabled"]:
            # vorher alles aus dem alten Cache auf die Platten verschieben
            self.flush(pool, wait=True)
            if self.used(pool)["files"]:
                raise RuntimeError("Der Cache konnte nicht vollständig geleert werden (Dateien in Benutzung?) – "
                                   "bitte später erneut versuchen")
        with self.settings.lock:
            pool["cache"] = new
            self.settings.save()
        if new["enabled"]:
            os.makedirs(cache_dir(pool), exist_ok=True)
        if layout_change:
            self.pools.remount(pool)          # SMB-Verbindungen zu diesem Pool werden kurz unterbrochen
        return new

    def targets(self):
        """Mögliche Cache-Ziele: System-SSD und eingehängte, freie Platten (SSDs zuerst)."""
        used = {m["uuid"] for p in self.settings.data["pools"] for m in p["members"]}
        res = []
        try:
            st = os.statvfs(config.DATA_DIR)
            res.append({"id": SYSTEM, "name": "System-SSD von Home Assistant", "ssd": True,
                        "free": st.f_bavail * st.f_frsize, "total": st.f_blocks * st.f_frsize})
        except OSError:
            pass
        for d in self.disks.disks:
            if d["system"] or d.get("network") or d.get("raid_member"):
                continue
            for p in d["partitions"]:
                if p["mountpoint"] and p["uuid"] not in used and p.get("usage"):
                    res.append({"id": p["uuid"], "name": f"{d['model'] or d['name']} · {p['label'] or p['name']}",
                                "ssd": not d.get("rotational", True), "free": p["usage"]["free"],
                                "total": p["usage"]["total"], "writable": self.disks.is_writable(p["uuid"])})
        res[1:] = sorted(res[1:], key=lambda x: (not x["ssd"], x["name"]))
        return res

    def active(self, pool):
        c = pool_cache_cfg(pool)
        if not c["enabled"] or self.options["read_only"]:
            return False
        if c["target"] != SYSTEM:
            _, part = self.disks.partition(c["target"])
            if not part or not part["mountpoint"] or not self.disks.is_writable(c["target"]):
                return False
        return True

    # ------------------------------------------------------------ Belegung
    def used(self, pool):
        d = cache_dir(pool)
        size = files = 0
        oldest = None
        for root, dirs, names in os.walk(d):
            for n in names:
                if n.startswith(TMP):
                    continue
                try:
                    st = os.stat(os.path.join(root, n))
                except OSError:
                    continue
                size += st.st_size
                files += 1
                oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
        try:
            st = os.statvfs(device_root(pool) if os.path.isdir(device_root(pool)) else config.DATA_DIR)
            free, total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
        except OSError:
            free = total = 0
        return {"bytes": size, "files": files, "oldest": int(oldest) if oldest else None,
                "device_free": free, "device_total": total}

    # ------------------------------------------------------------ Mover
    @staticmethod
    def _open_files():
        """Pfade, die gerade von Samba (oder anderen Prozessen im Container) geöffnet sind."""
        res = set()
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            fd_dir = f"/proc/{pid}/fd"
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        res.add(os.readlink(os.path.join(fd_dir, fd)))
                    except OSError:
                        pass
            except OSError:
                pass
        return res

    # ------------------------------------------------------------ Paralleles Verschieben
    def _move_parallel(self, pool, todo, members, st):
        """Mehrere Dateien gleichzeitig auf verschiedene Platten schreiben. Jede Datei wird direkt in
        der Soll-Anzahl Kopien auf die Platten geschrieben (einmal von der SSD lesen, auf 1–3 Platten
        gleichzeitig schreiben). Eine Platte bearbeitet immer nur eine Datei – so arbeiten alle
        Platten parallel, statt dass sich mehrere Kopien auf einer Platte gegenseitig ausbremsen."""
        rep = self.rep
        min_free = rep.cfg().get("min_free_gb", 20) * 1e9
        busy = set()
        cond = threading.Condition()
        jobs = list(todo)
        workers = max(1, min(len(members), 4))

        def pick(rel, size):
            """Zielplatten wählen (unter cond). None = gerade keine frei, [] = unmöglich."""
            t = max(1, min(rep._target(pool, rel), len(members)))
            have = [m for m in members if os.path.exists(os.path.join(m["branch"], rel))]
            rest = sorted((m for m in members if m not in have), key=lambda m: -m["free"])
            chosen = list(have[:t])
            for m in rest:
                if len(chosen) >= t:
                    break
                if m["free"] - size > min_free:
                    chosen.append(m)
            if not chosen:
                return []
            if any(m["uuid"] in busy for m in chosen):
                # andere freie Platten mit genug Platz bevorzugen
                alt = list(have[:t]) + [m for m in rest if m["uuid"] not in busy and m["free"] - size > min_free]
                alt = alt[:len(chosen)]
                if len(alt) == len(chosen) and not any(m["uuid"] in busy for m in alt):
                    return alt
                return None
            return chosen

        def worker():
            while not self.stop.is_set() and not self.paused_reason:
                with cond:
                    item, targets = None, None
                    while jobs and item is None:
                        for i, (rel, size) in enumerate(jobs):
                            tg = pick(rel, size)
                            if tg == []:
                                jobs.pop(i)
                                log.info("Cache: kein Platz für %s", rel)
                                break
                            if tg:
                                item, targets = jobs.pop(i), tg
                                break
                        else:
                            if not cond.wait(5):
                                continue
                    if item is None:
                        return
                    for m in targets:
                        busy.add(m["uuid"])
                    st["active"].append(item[0])
                    st["current"] = item[0]
                try:
                    self._move_multi(pool, item[0], targets)
                    with cond:
                        st["done"] += 1
                        st["moved_bytes"] += item[1]
                        for m in targets:
                            m["free"] -= item[1]
                except InterruptedError:
                    return
                except OSError as err:
                    log.info("Cache: %s nicht verschoben: %s", item[0], err)
                finally:
                    with cond:
                        for m in targets:
                            busy.discard(m["uuid"])
                        if item[0] in st["active"]:
                            st["active"].remove(item[0])
                        cond.notify_all()

        threads = [threading.Thread(target=worker, daemon=True, name=f"mover-{i}") for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _move_multi(self, pool, rel, targets):
        src = os.path.join(cache_dir(pool), rel)
        st = os.stat(src)
        tmps = []
        try:
            outs = []
            for m in targets:
                dst = os.path.join(m["branch"], rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                tmp = os.path.join(os.path.dirname(dst), f"{TMP}{uuidlib.uuid4().hex[:8]}-{os.path.basename(dst)}"[:250])
                tmps.append((tmp, dst))
                outs.append(open(tmp, "wb"))  # noqa: SIM115
            try:
                with open(src, "rb") as fi:
                    while True:
                        if self.stop.is_set():
                            raise InterruptedError
                        buf = fi.read(4 << 20)
                        if not buf:
                            break
                        for fo in outs:
                            fo.write(buf)
                for fo in outs:
                    fo.flush()
                    os.fsync(fo.fileno())
            finally:
                for fo in outs:
                    fo.close()
            for tmp, _ in tmps:
                if os.path.getsize(tmp) != st.st_size:
                    raise OSError("Größe stimmt nicht")
                os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            st2 = os.stat(src)
            if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                raise OSError("Datei wurde während des Verschiebens geändert")
            for tmp, dst in tmps:
                os.replace(tmp, dst)
                try:
                    self.rep._ignore_path(dst, 60)
                except Exception:  # noqa: BLE001
                    pass
            tmps = []
        except BaseException:
            for tmp, _ in tmps:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise
        os.remove(src)
        if self.on_moved:
            try:
                self.on_moved(pool["id"], rel)
            except Exception:  # noqa: BLE001
                pass
        return st.st_size

    def _move_one(self, pool, rel, base):
        src = os.path.join(cache_dir(pool), rel)
        dst = os.path.join(base, rel)
        st = os.stat(src)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = os.path.join(os.path.dirname(dst), f"{TMP}{uuidlib.uuid4().hex[:8]}-{os.path.basename(dst)}"[:250])
        try:
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    if self.stop.is_set():
                        raise InterruptedError
                    buf = fi.read(4 << 20)
                    if not buf:
                        break
                    fo.write(buf)
                fo.flush()
                os.fsync(fo.fileno())
            if os.path.getsize(tmp) != st.st_size:
                raise OSError("Größe stimmt nicht")
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            # während des Kopierens verändert? Dann beim nächsten Durchlauf erneut
            st2 = os.stat(src)
            if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                raise OSError("Datei wurde während des Verschiebens geändert")
            os.replace(tmp, dst)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        os.remove(src)
        if self.on_moved:
            try:
                self.on_moved(pool["id"], rel)
            except Exception:  # noqa: BLE001
                pass
        return st.st_size

    def move(self, pool, mode="auto"):
        """mode: auto (nach Alter/Füllstand), all (alles, z. B. nachts oder „Jetzt verschieben“)."""
        pid = pool["id"]
        c = pool_cache_cfg(pool)
        root = cache_dir(pool)
        base = self.pools.base_mountpoint(pool)
        if not os.path.isdir(root) or not self.pools.is_mounted(pool):
            return
        st = self.state.setdefault(pid, {})
        files = []
        hold = c.get("hold_downloads", True)
        dir_age = {}
        now0 = time.time()
        for r, _, names in os.walk(root):
            if hold:
                # Ordner gilt als „in Arbeit“, solange darin Dateien angelegt/umbenannt/gelöscht werden
                # (z. B. Download läuft oder JDownloader entpackt gerade)
                try:
                    dir_age[r] = now0 - os.stat(r).st_mtime
                except OSError:
                    dir_age[r] = 0
            for n in names:
                if n.startswith(TMP):
                    continue
                p = os.path.join(r, n)
                try:
                    s = os.stat(p)
                except OSError:
                    continue
                files.append((max(s.st_mtime, s.st_ctime), s.st_size, os.path.relpath(p, root)))
        if not files:
            st.update(running=False)
            return
        files.sort()                       # älteste zuerst
        now = time.time()
        total = sum(f[1] for f in files)
        limit = c["max_gb"] * 1e9
        dev = self.used(pool)
        pressure = total > limit * c["move_at_pct"] / 100 or dev["device_free"] < c["min_free_gb"] * 1e9 * 1.2
        target_bytes = limit * 0.5 if pressure else 0
        todo = []
        remaining = total
        quiet = max(c["move_after_min"], 5) * 60
        held = []
        for age_ts, size, rel in files:
            age = now - age_ts
            if age < 60:
                continue                   # gerade erst geschrieben
            if hold and mode != "all":
                name = os.path.basename(rel).lower()
                if any(fnmatch.fnmatch(name, pat) for pat in HOLD_PATTERNS) and age < c.get("hold_hours", 24) * 3600:
                    held.append((rel, size))       # Archivteil: bleibt, außer die SSD wird voll
                    continue
                if dir_age.get(os.path.join(root, os.path.dirname(rel)).rstrip("/"), quiet) < quiet and not pressure:
                    continue                       # im Ordner tut sich noch etwas (Download/Entpacken)
            if mode == "all" or (c["move_after_min"] and age >= c["move_after_min"] * 60) or \
                    (pressure and remaining > target_bytes):
                todo.append((rel, size))
                remaining -= size
        if pressure and remaining > target_bytes:
            # SSD wird voll: dann auch Archivteile verschieben (älteste zuerst)
            for rel, size in held:
                if remaining <= target_bytes:
                    break
                todo.append((rel, size))
                remaining -= size
        if not todo:
            return
        open_files = self._open_files()
        todo = [(rel, size) for rel, size in todo if os.path.join(root, rel) not in open_files]
        st.update(running=True, started=int(time.time()), total=len(todo), done=0, moved_bytes=0,
                  current=None, active=[])
        rep = getattr(self, "rep", None)
        members = [m for m in rep.members(pool) if m["writable"]] if rep else []
        if len(members) >= 1:
            self._move_parallel(pool, todo, members, st)
        else:
            for rel, size in todo:
                if self.stop.is_set() or self.paused_reason:
                    break
                try:
                    st["current"] = rel
                    self._move_one(pool, rel, base)
                    st["done"] += 1
                    st["moved_bytes"] += size
                except InterruptedError:
                    break
                except OSError as err:
                    log.info("Cache: %s nicht verschoben: %s", rel, err)
        # leere Ordner im Cache aufräumen
        for r, dirs, names in os.walk(root, topdown=False):
            if r != root and not os.listdir(r):
                try:
                    os.rmdir(r)
                except OSError:
                    pass
        st.update(running=False, last=int(time.time()), current=None)
        if st["done"]:
            self.activity.appendleft({"t": int(time.time()), "pool": pid, "files": st["done"], "bytes": st["moved_bytes"]})
            log.info("Cache %s: %d Dateien (%d MB) auf die Platten verschoben", pid, st["done"], st["moved_bytes"] // 1_000_000)

    def flush(self, pool, wait=False):
        if wait:
            self.move(pool, mode="all")
        else:
            self.flush_requests.add(pool["id"])

    def loop(self):
        lower_priority()
        last_day = {}
        while not self.stop.wait(30):
            if self.paused_reason:
                continue
            now = dt.datetime.now()
            for pool in list(self.settings.data["pools"]):
                if not self.active(pool):
                    continue
                c = pool_cache_cfg(pool)
                try:
                    if pool["id"] in self.flush_requests:
                        self.flush_requests.discard(pool["id"])
                        self.move(pool, mode="all")
                    elif now.strftime("%H:%M") == c["move_time"] and last_day.get(pool["id"]) != now.date():
                        last_day[pool["id"]] = now.date()
                        self.move(pool, mode="all")
                    else:
                        self.move(pool, mode="auto")
                except Exception:  # noqa: BLE001
                    log.exception("Cache-Mover %s", pool["id"])

    def status(self, pool):
        c = pool_cache_cfg(pool)
        res = {"config": c, "active": self.active(pool), "dir": cache_dir(pool),
               "mover": self.state.get(pool["id"], {})}
        if c["enabled"]:
            res["usage"] = self.used(pool)
        return res
