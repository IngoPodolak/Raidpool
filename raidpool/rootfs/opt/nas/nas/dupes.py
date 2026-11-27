"""Duplikatsuche: findet inhaltsgleiche Dateien (z. B. doppelt heruntergeladen).

Kopien, die der Pool selbst zur Duplizierung anlegt, zählen nicht – gearbeitet wird auf
logischen Pfaden (eine Datei im Pool = ein Eintrag, egal auf wie vielen Platten sie liegt).

Ablauf, damit möglichst wenig gelesen wird:
  1. Kandidaten nach Größe (Dateien mit einzigartiger Größe können keine Duplikate sein)
  2. Schnellvergleich: Hash über Anfang, Mitte und Ende (je 64 KiB)
  3. Vollvergleich: Hash über den kompletten Inhalt (BLAKE2b)
  4. Vor dem Löschen zusätzlich Byte-für-Byte-Vergleich mit einer verbleibenden Datei
Hashes werden zwischengespeichert (Pfad + Größe + Änderungszeit) – erneute Scans lesen nur Neues.
"""
import ctypes
import datetime as dt
import hashlib
import logging
import os
import platform
import shutil
import threading
import time

from . import config
from .index import connect

log = logging.getLogger("nas")

CHUNK = 1 << 20
PARTIAL = 64 * 1024
JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "folder.jpg", "albumartsmall.jpg"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS hashes (
  pool TEXT, path TEXT, size INTEGER, mtime INTEGER, partial TEXT, full TEXT,
  PRIMARY KEY (pool, path)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS dup_results (
  hash TEXT, size INTEGER, pool TEXT, path TEXT, mtime INTEGER, disks TEXT,
  PRIMARY KEY (hash, pool, path)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS dup_ignore (
  hash TEXT PRIMARY KEY, kind TEXT, created INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS dup_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started INTEGER, finished INTEGER, trigger TEXT,
  status TEXT, groups INTEGER, files INTEGER, wasted INTEGER, read_bytes INTEGER, error TEXT);
"""

DEFAULTS = {
    "mode": "manual",            # manual | schedule | auto
    "days": [0, 1, 2, 3, 4, 5, 6],
    "time": "03:00",
    "auto_interval_hours": 24,
    "idle_cpu_percent": 25,
    "idle_io_mb_s": 2,
    "idle_minutes": 5,
    "min_size_kb": 100,
    "max_mb_s": 60,
    "refresh_index": True,
    "excludes": [],
    "use_trash": True,
    "trash_days": 30,
}


# ---------------------------------------------------------------- Priorität
def lower_priority():
    """Aktuellen Thread auf niedrigste CPU- und I/O-Priorität setzen (Linux)."""
    tid = threading.get_native_id()
    try:
        os.setpriority(os.PRIO_PROCESS, tid, 19)
    except (OSError, AttributeError):
        pass
    nr = {"x86_64": 251, "aarch64": 30}.get(platform.machine())
    if nr:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.syscall(nr, 1, tid, 3 << 13)  # IOPRIO_WHO_PROCESS, Klasse IDLE
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- Leerlauf-Erkennung
class IdleMonitor:
    """Misst CPU-Auslastung und Platten-I/O des Rechners (ohne die eigenen Lesezugriffe)."""

    def __init__(self):
        self.cpu = 0.0
        self.io = 0.0
        self.own_bytes = 0
        self.idle_since = None
        self._prev = None
        self.lock = threading.Lock()

    @staticmethod
    def _sample():
        with open("/proc/stat", encoding="utf-8") as fh:
            vals = list(map(int, fh.readline().split()[1:]))
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals[:8])
        io = 0
        try:
            with open("/proc/diskstats", encoding="utf-8") as fh:
                for line in fh:
                    p = line.split()
                    name = p[2]
                    if (name[:2] in ("sd", "vd", "hd") and not name[-1].isdigit()) or \
                            (name.startswith("nvme") and "p" not in name[4:]):
                        io += (int(p[5]) + int(p[9])) * 512
        except OSError:
            pass
        return time.time(), idle, total, io

    def update(self, cfg):
        try:
            s = self._sample()
        except OSError:
            return
        with self.lock:
            own = self.own_bytes
        if self._prev:
            t0, i0, tot0, io0, own0 = self._prev
            dtot = s[2] - tot0
            self.cpu = 100.0 * (1 - (s[1] - i0) / dtot) if dtot > 0 else 0.0
            secs = max(s[0] - t0, 1e-3)
            self.io = max(0.0, (s[3] - io0) - (own - own0)) / secs / 1e6
        self._prev = (*s, own)
        busy = self.cpu > cfg["idle_cpu_percent"] or self.io > cfg["idle_io_mb_s"]
        if busy:
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = time.time()

    def idle_for(self):
        return 0 if self.idle_since is None else time.time() - self.idle_since

    def count(self, n):
        with self.lock:
            self.own_bytes += n


# ---------------------------------------------------------------- Duplikatsuche
class DupeFinder:
    def __init__(self, options, settings, pools, index):
        self.options = options
        self.settings = settings
        self.pools = pools
        self.index = index
        self.db = connect()
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.idle = IdleMonitor()
        self.thread = None
        self.cancel = threading.Event()
        self.progress = {"running": False}
        self.once_ignored = set()   # Hashes, die bis zum nächsten Scan ausgeblendet sind
        self.stop = threading.Event()

    # ------------------------------------------------------------ Einstellungen
    def cfg(self):
        c = dict(DEFAULTS)
        c.update(self.settings.data.get("dupes") or {})
        return c

    def set_cfg(self, values):
        allowed = set(DEFAULTS)
        with self.settings.lock:
            cur = self.settings.data.setdefault("dupes", {})
            for k, v in values.items():
                if k in allowed:
                    cur[k] = v
            self.settings.save()
        return self.cfg()

    # ------------------------------------------------------------ Pfade
    def physical_paths(self, pool_id, rel, disks_csv):
        pool = self.settings.pool(pool_id)
        if not pool:
            return []
        pp = {m["uuid"]: m["poolpart"] for m in pool["members"]}
        res = []
        for u in (disks_csv or "").split(","):
            if u in pp:
                p = os.path.join(config.DISK_MNT, u, pp[u], rel)
                if os.path.isfile(p):
                    res.append((u, pp[u], p))
        return res

    def _excluded(self, pool_id, path, cfg):
        name = path.rsplit("/", 1)[-1].lower()
        if name in JUNK_NAMES:
            return True
        for ex in cfg["excludes"]:
            ex = ex.strip().strip("/")
            if not ex:
                continue
            ex_pool, _, ex_path = ex.partition(":") if ":" in ex else ("", "", ex)
            if ex_pool and ex_pool != pool_id:
                continue
            if path == ex_path or path.startswith(ex_path + "/"):
                return True
        return False

    # ------------------------------------------------------------ Lesen
    def _read_hash(self, path, ranges, cfg, pausable):
        h = hashlib.blake2b(digest_size=32)
        limit = max(1, cfg["max_mb_s"]) * 1e6
        with open(path, "rb", buffering=0) as fh:
            fd = fh.fileno()
            for start, length in ranges:
                fh.seek(start)
                left = length
                while left > 0:
                    if self.cancel.is_set():
                        raise InterruptedError
                    if pausable:
                        self._wait_idle(cfg)
                    t0 = time.monotonic()
                    data = fh.read(min(CHUNK, left))
                    if not data:
                        break
                    h.update(data)
                    left -= len(data)
                    n = len(data)
                    self.idle.count(n)
                    self.progress["read_bytes"] = self.progress.get("read_bytes", 0) + n
                    took = time.monotonic() - t0
                    wait = n / limit - took
                    if wait > 0:
                        time.sleep(wait)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)  # Cache nicht verdrängen
            except (OSError, AttributeError):
                pass
        return h.hexdigest()

    def _wait_idle(self, cfg):
        """Im Automatik-Modus pausieren, solange der Rechner beschäftigt ist."""
        while not self.cancel.is_set():
            self.idle.update(cfg)
            if self.idle.cpu <= cfg["idle_cpu_percent"] * 1.5 and self.idle.io <= cfg["idle_io_mb_s"] * 2:
                self.progress["paused"] = False
                return
            self.progress["paused"] = True
            time.sleep(15)

    # ------------------------------------------------------------ Scan
    def start(self, trigger="manuell"):
        if self.thread and self.thread.is_alive():
            raise ValueError("Duplikatsuche läuft bereits")
        self.cancel.clear()
        self.thread = threading.Thread(target=self._run, args=(trigger,), daemon=True, name="dupes")
        self.thread.start()

    def stop_scan(self):
        self.cancel.set()

    def _run(self, trigger):
        lower_priority()
        cfg = self.cfg()
        pausable = trigger == "automatisch"
        p = self.progress = {"running": True, "trigger": trigger, "phase": "Index aktualisieren",
                             "started": int(time.time()), "done": 0, "total": 0,
                             "read_bytes": 0, "paused": False, "groups": 0}
        with self.lock:
            cur = self.db.execute("INSERT INTO dup_runs (started, trigger, status) VALUES (?,?,?)",
                                  (p["started"], trigger, "läuft"))
            run_id = cur.lastrowid
            self.db.commit()
        status, err = "fertig", None
        try:
            if cfg["refresh_index"]:
                for pool in list(self.settings.data["pools"]):
                    if self.cancel.is_set():
                        raise InterruptedError
                    branches = [(u, b) for u, b, ok in self.pools.branches(pool) if ok]
                    self.index.scan(pool, branches)
            self._find(cfg, pausable)
        except InterruptedError:
            status = "abgebrochen"
        except Exception as e:  # noqa: BLE001
            log.exception("Duplikatsuche fehlgeschlagen")
            status, err = "fehler", str(e)
        finally:
            stats = self.summary()
            with self.lock:
                self.db.execute("UPDATE dup_runs SET finished=?, status=?, groups=?, files=?, wasted=?, "
                                "read_bytes=?, error=? WHERE id=?",
                                (int(time.time()), status, stats["groups"], stats["files"], stats["wasted"],
                                 p.get("read_bytes", 0), err, run_id))
                self.db.commit()
            p["running"] = False
            log.info("Duplikatsuche %s: %s Gruppen, %s MB gelesen", status, stats["groups"],
                     p.get("read_bytes", 0) // 1_000_000)

    def _find(self, cfg, pausable):
        p = self.progress
        min_size = max(1, int(cfg["min_size_kb"]) * 1024)
        p["phase"] = "Kandidaten suchen"
        with self.lock:
            rows = self.db.execute(
                "SELECT pool, path, size, mtime, disks FROM files WHERE size >= ? AND size IN "
                "(SELECT size FROM files WHERE size >= ? GROUP BY size HAVING count(*) > 1) "
                "ORDER BY size DESC", (min_size, min_size)).fetchall()
            cache = {(r[0], r[1]): (r[2], r[3], r[4], r[5]) for r in
                     self.db.execute("SELECT pool, path, size, mtime, partial, full FROM hashes")}
        by_size = {}
        for r in rows:
            if self._excluded(r[0], r[1], cfg):
                continue
            by_size.setdefault(r[2], []).append(tuple(r))
        by_size = {s: lst for s, lst in by_size.items() if len(lst) > 1}

        def cached(f, kind):
            c = cache.get((f[0], f[1]))
            if c and c[0] == f[2] and c[1] == f[3]:
                return c[2] if kind == "partial" else c[3]
            return None

        def store(f, partial, full):
            with self.lock:
                self.db.execute("INSERT OR REPLACE INTO hashes VALUES (?,?,?,?,?,?)",
                                (f[0], f[1], f[2], f[3], partial, full))
            cache[(f[0], f[1])] = (f[2], f[3], partial, full)

        # Gleiche Namen zuerst prüfen (häufigster Fall: doppelt heruntergeladen)
        def name_first(lst):
            names = {}
            for f in lst:
                names.setdefault(f[1].rsplit("/", 1)[-1].lower(), []).append(f)
            return sorted(lst, key=lambda f: -len(names[f[1].rsplit("/", 1)[-1].lower()]))

        # Schritt 2: Schnellvergleich
        p["phase"] = "Schnellvergleich (Anfang/Mitte/Ende)"
        p["total"] = sum(len(v) for v in by_size.values())
        p["done"] = 0
        partial_groups = {}
        for size, lst in by_size.items():
            for f in name_first(lst):
                p["done"] += 1
                h = cached(f, "partial")
                if h is None:
                    phys = self.physical_paths(f[0], f[1], f[4])
                    if not phys:
                        continue
                    if size <= 3 * PARTIAL:
                        ranges = [(0, size)]
                    else:
                        ranges = [(0, PARTIAL), (size // 2 - PARTIAL // 2, PARTIAL), (size - PARTIAL, PARTIAL)]
                    try:
                        h = self._read_hash(phys[0][2], ranges, cfg, pausable)
                    except OSError as err:
                        log.debug("Nicht lesbar %s: %s", f[1], err)
                        continue
                    full = h if size <= 3 * PARTIAL else cached(f, "full")
                    store(f, h, full)
                partial_groups.setdefault((size, h), []).append(f)
            with self.lock:
                self.db.commit()

        # Schritt 3: Vollvergleich
        cands = {k: v for k, v in partial_groups.items() if len(v) > 1}
        p["phase"] = "Vollvergleich"
        p["total"] = sum(len(v) for v in cands.values())
        p["total_bytes"] = sum(k[0] * len(v) for k, v in cands.items())
        p["done"] = 0
        groups = {}
        for (size, ph), lst in cands.items():
            for f in lst:
                p["done"] += 1
                h = cached(f, "full")
                if h is None:
                    phys = self.physical_paths(f[0], f[1], f[4])
                    if not phys:
                        continue
                    try:
                        h = self._read_hash(phys[0][2], [(0, size)], cfg, pausable)
                    except OSError as err:
                        log.debug("Nicht lesbar %s: %s", f[1], err)
                        continue
                    store(f, ph, h)
                groups.setdefault(h, []).append(f)
            with self.lock:
                self.db.commit()
            p["groups"] = sum(1 for v in groups.values() if len(v) > 1)

        p["phase"] = "Ergebnis speichern"
        out = []
        for h, lst in groups.items():
            if len(lst) > 1:
                for f in lst:
                    out.append((h, f[2], f[0], f[1], f[3], f[4]))
        with self.lock:
            self.db.execute("DELETE FROM dup_results")
            self.db.executemany("INSERT OR REPLACE INTO dup_results VALUES (?,?,?,?,?,?)", out)
            self.db.commit()
        self.once_ignored.clear()

    # ------------------------------------------------------------ Ergebnisse
    def _ignored(self):
        with self.lock:
            perm = {r[0] for r in self.db.execute("SELECT hash FROM dup_ignore")}
        return perm | self.once_ignored

    def summary(self):
        ign = self._ignored()
        with self.lock:
            rows = self.db.execute("SELECT hash, size, count(*) FROM dup_results GROUP BY hash").fetchall()
        groups = [(h, s, c) for h, s, c in rows if h not in ign]
        return {"groups": len(groups), "files": sum(c for _, _, c in groups),
                "wasted": sum(s * (c - 1) for _, s, c in groups),
                "ignored": sum(1 for h, _, _ in rows if h in ign)}

    def groups(self, offset=0, limit=50, search="", show_ignored=False):
        ign_perm = set()
        with self.lock:
            ign_perm = {r[0] for r in self.db.execute("SELECT hash FROM dup_ignore")}
            rows = self.db.execute("SELECT * FROM dup_results ORDER BY size DESC, hash, pool, path").fetchall()
        groups = {}
        order = []
        for r in rows:
            g = groups.get(r["hash"])
            if g is None:
                g = groups[r["hash"]] = {"hash": r["hash"], "size": r["size"], "files": []}
                order.append(r["hash"])
            g["files"].append({"pool": r["pool"], "pool_name": (self.settings.pool(r["pool"]) or {}).get("name", r["pool"]),
                               "path": r["path"], "mtime": r["mtime"], "disks": r["disks"]})
        res = []
        s = search.lower().strip()
        for h in order:
            g = groups[h]
            hidden = h in ign_perm or h in self.once_ignored
            if hidden != bool(show_ignored):
                continue
            if s and not any(s in f["path"].lower() for f in g["files"]):
                continue
            names = {f["path"].rsplit("/", 1)[-1].lower() for f in g["files"]}
            g["same_name"] = len(names) == 1
            g["count"] = len(g["files"])
            g["wasted"] = g["size"] * (g["count"] - 1)
            g["ignored"] = "dauerhaft" if h in ign_perm else ("einmalig" if h in self.once_ignored else None)
            res.append(g)
        res.sort(key=lambda g: -g["wasted"])
        return {"total": len(res), "groups": res[offset:offset + limit]}

    def ignore(self, hashes, kind, note=""):
        if kind == "once":
            self.once_ignored.update(hashes)
        elif kind == "forever":
            with self.lock:
                self.db.executemany("INSERT OR REPLACE INTO dup_ignore VALUES (?,?,?,?)",
                                    [(h, "dauerhaft", int(time.time()), note) for h in hashes])
                self.db.commit()
        elif kind == "unignore":
            self.once_ignored.difference_update(hashes)
            with self.lock:
                self.db.executemany("DELETE FROM dup_ignore WHERE hash=?", [(h,) for h in hashes])
                self.db.commit()

    def last_run(self):
        with self.lock:
            r = self.db.execute("SELECT * FROM dup_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------ Löschen
    @staticmethod
    def _same_bytes(a, b):
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x, y = fa.read(CHUNK), fb.read(CHUNK)
                if x != y:
                    return False
                if not x:
                    return True

    def delete(self, items):
        """items: [{pool, path}] – löscht die gewählten Dateien (alle Pool-Kopien davon)."""
        if self.options["read_only"]:
            raise ValueError("Schreibschutz aktiv – zum Löschen 'read_only' in den Add-on-Optionen ausschalten")
        cfg = self.cfg()
        want = {(i["pool"], i["path"]) for i in items}
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM dup_results")]
        by_hash = {}
        for r in rows:
            by_hash.setdefault(r["hash"], []).append(r)
        results = {"deleted": 0, "freed": 0, "errors": []}
        touched = set()
        for h, files in by_hash.items():
            dels = [f for f in files if (f["pool"], f["path"]) in want]
            keep = [f for f in files if (f["pool"], f["path"]) not in want]
            if not dels:
                continue
            if not keep:
                results["errors"].append(f"Mindestens eine Datei muss bleiben: {dels[0]['path']}")
                continue
            keep_phys = None
            for k in keep:
                ph = self.physical_paths(k["pool"], k["path"], k["disks"])
                if ph:
                    keep_phys = ph[0][2]
                    break
            if not keep_phys:
                results["errors"].append(f"Verbleibende Datei nicht gefunden: {keep[0]['path']}")
                continue
            for f in dels:
                phys = self.physical_paths(f["pool"], f["path"], f["disks"])
                if not phys:
                    results["errors"].append(f"Nicht gefunden: {f['path']}")
                    continue
                try:
                    if os.path.getsize(phys[0][2]) != f["size"] or not self._same_bytes(phys[0][2], keep_phys):
                        results["errors"].append(f"Inhalt geändert, übersprungen: {f['path']}")
                        continue
                    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                    for uuid, pp, path in phys:   # alle Pool-Kopien dieser Datei
                        if cfg["use_trash"]:
                            dest = os.path.join(config.DISK_MNT, uuid, pp, ".nas-trash", stamp, f["path"])
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            os.rename(path, dest)
                        else:
                            os.remove(path)
                except OSError as err:
                    results["errors"].append(f"{f['path']}: {err}")
                    continue
                results["deleted"] += 1
                results["freed"] += f["size"] * len(phys)
                touched.add(f["pool"])
                with self.lock:
                    self.db.execute("DELETE FROM dup_results WHERE pool=? AND path=?", (f["pool"], f["path"]))
                    self.db.execute("DELETE FROM files WHERE pool=? AND path=?", (f["pool"], f["path"]))
                    self.db.execute("DELETE FROM hashes WHERE pool=? AND path=?", (f["pool"], f["path"]))
                    self.db.commit()
        # Gruppen mit nur noch einer Datei entfernen
        with self.lock:
            self.db.execute("DELETE FROM dup_results WHERE hash IN "
                            "(SELECT hash FROM dup_results GROUP BY hash HAVING count(*) < 2)")
            self.db.commit()
        for pid in touched:
            pool = self.settings.pool(pid)
            if pool:
                self.index.aggregate(pool)
        return results

    # ------------------------------------------------------------ Papierkorb
    def trash_info(self):
        total, count = 0, 0
        for pool in self.settings.data["pools"]:
            for _, branch, ok in self.pools.branches(pool):
                t = os.path.join(branch, ".nas-trash")
                if ok and os.path.isdir(t):
                    for root, _, files in os.walk(t):
                        for name in files:
                            try:
                                total += os.path.getsize(os.path.join(root, name))
                                count += 1
                            except OSError:
                                pass
        return {"bytes": total, "files": count}

    def purge_trash(self, older_than_days=None):
        if self.options["read_only"]:
            return
        days = self.cfg()["trash_days"] if older_than_days is None else older_than_days
        limit = dt.datetime.now() - dt.timedelta(days=days)
        for pool in self.settings.data["pools"]:
            for _, branch, ok in self.pools.branches(pool):
                t = os.path.join(branch, ".nas-trash")
                if not ok or not os.path.isdir(t):
                    continue
                for name in os.listdir(t):
                    try:
                        when = dt.datetime.strptime(name, "%Y%m%d-%H%M%S")
                    except ValueError:
                        continue
                    if when <= limit:
                        shutil.rmtree(os.path.join(t, name), ignore_errors=True)

    # ------------------------------------------------------------ Planer
    def scheduler_loop(self):
        last_sched = None
        last_purge = 0
        while not self.stop.wait(30):
            cfg = self.cfg()
            try:
                self.idle.update(cfg)
                running = self.thread and self.thread.is_alive()
                now = dt.datetime.now()
                if cfg["mode"] == "schedule" and not running:
                    key = now.strftime("%Y-%m-%d ") + cfg["time"]
                    if now.weekday() in cfg["days"] and now.strftime("%H:%M") == cfg["time"] and key != last_sched:
                        last_sched = key
                        self.start("zeitplan")
                elif cfg["mode"] == "auto" and not running:
                    last = self.last_run()
                    due = not last or (time.time() - (last["finished"] or last["started"] or 0)
                                       >= cfg["auto_interval_hours"] * 3600)
                    if due and self.idle.idle_for() >= cfg["idle_minutes"] * 60:
                        self.start("automatisch")
                if time.time() - last_purge > 6 * 3600:
                    last_purge = time.time()
                    self.purge_trash()
            except Exception:  # noqa: BLE001
                log.exception("Planer Duplikatsuche")

    def status(self):
        p = dict(self.progress)
        return {"progress": p, "last": self.last_run(), "summary": self.summary(), "config": self.cfg(),
                "idle": {"cpu": round(self.idle.cpu, 1), "io_mb_s": round(self.idle.io, 2),
                         "idle_for": int(self.idle.idle_for())},
                "read_only": self.options["read_only"]}
