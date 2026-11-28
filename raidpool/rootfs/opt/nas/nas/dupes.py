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
import json
import logging
import os
import re
import platform
import shutil
import threading
import time

from . import config
from .index import connect

log = logging.getLogger("nas")

CHUNK = 1 << 20
PARTIAL = 64 * 1024
SAMPLE_FULL = 1 << 20        # Dateien bis 1 MB beim Stichprobenvergleich ganz lesen
SAMPLE_EDGE = 128 * 1024     # Anfang und Ende
SAMPLE_POINTS = 8            # weitere Stellen dazwischen (je 64 KB)
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
  status TEXT, groups INTEGER, files INTEGER, wasted INTEGER, read_bytes INTEGER, error TEXT, scope TEXT);
"""

# Ordnernamen, die auf unsortierte Sammelordner hindeuten (für Vorschläge)
UNSORTED_HINTS = ("unsortiert", "unsorted", "sortieren", "import", "download", "neuer ordner", "new folder",
                  "dcim", "camera", "kamera", "handy", "whatsapp", "backup", "sicherung", "temp", "tmp", "alt",
                  "kopie", "copy", "inbox", "eingang", "upload", "sync")
# Dateinamen von Kopien: „x (1).jpg“, „x - Kopie.jpg“, „x - Kopie (2).jpg“, „x copy 3.jpg“, „x_1.jpg“ nicht (zu unsicher)
COPY_RE = re.compile(r"(\s\(\d+\)|\s-\s(kopie|copy)(\s\(\d+\))?|\s(kopie|copy)(\s\d+)?)(\.[^.]+)?$", re.I)


def _parent(path):
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _in_folder(f, folder):
    """folder: {pool, path, sub}; sub=True schließt Unterordner ein."""
    if f["pool"] != folder["pool"]:
        return False
    d = _parent(f["path"])
    p = folder["path"]
    if not p:
        return True if folder.get("sub", True) else d == ""
    return d == p or (folder.get("sub", True) and d.startswith(p + "/"))


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
    "verify": "sample",          # sample: Stichproben (vor dem Löschen wird vollständig verglichen) | full
    "excludes": [],
    "sources": [],               # zusätzliche Quellen außerhalb von Pools (Platten/Netzwerkfreigaben, uuid)
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
        try:   # ältere Datenbanken: Spalte für Stichproben nachrüsten
            self.db.execute("ALTER TABLE hashes ADD COLUMN sample TEXT")
            self.db.commit()
        except Exception:  # noqa: BLE001
            pass
        try:   # ältere Datenbanken: Spalte für gezielte Suche nachrüsten
            self.db.execute("ALTER TABLE dup_runs ADD COLUMN scope TEXT")
            self.db.commit()
        except Exception:  # noqa: BLE001
            pass
        self.scope = None
        self.copy_active = lambda: False
        self.lock = threading.Lock()
        self.idle = IdleMonitor()
        self.thread = None
        self.cancel = threading.Event()
        self.progress = {"running": False}
        self.once_ignored = set()   # Hashes, die bis zum nächsten Scan ausgeblendet sind
        self.pending = set()        # (pool, path), die gerade gelöscht werden – sofort ausgeblendet
        self.del_job = {"running": False}
        self.del_queue = []         # [(items, keys)] – weitere Löschaufträge, nacheinander abgearbeitet
        self.del_lock = threading.Lock()
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

    # ------------------------------------------------------------ Quellen
    def sources(self):
        """Zusätzliche Suchquellen als Pseudo-Pools: id 'src:<uuid>'."""
        out = []
        in_pools = {m["uuid"] for p in self.settings.data["pools"] for m in p["members"]}
        for uuid in self.cfg()["sources"]:
            if uuid in in_pools:
                continue   # ist inzwischen Pool-Mitglied – wird über den Pool durchsucht
            disk, part = self.pools.disks.partition(uuid)
            out.append({"id": "src:" + uuid, "uuid": uuid,
                        "name": (part or {}).get("label") or (disk or {}).get("model") or uuid,
                        "network": bool((disk or {}).get("network")),
                        "mounted": bool(part and part["mountpoint"]),
                        "root": os.path.join(config.DISK_MNT, uuid)})
        return out

    def name_of(self, pid):
        if pid.startswith("src:"):
            for s in self.sources():
                if s["id"] == pid:
                    return s["name"]
            return pid[4:]
        return (self.settings.pool(pid) or {}).get("name", pid)

    def roots(self):
        """Alle Wurzelverzeichnisse (Pool-Mitglieder und Quellen) – für Papierkorb."""
        res = []
        for pool in self.settings.data["pools"]:
            res += [b for _, b, ok in self.pools.branches(pool) if ok]
        res += [s["root"] for s in self.sources() if s["mounted"]]
        return res

    # ------------------------------------------------------------ Pfade
    def physical_paths(self, pool_id, rel, disks_csv):
        if pool_id.startswith("src:"):
            uuid = pool_id[4:]
            p = os.path.join(config.DISK_MNT, uuid, rel)
            return [(uuid, "", p)] if os.path.isfile(p) else []
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

    def _in_scope(self, pool_id, path):
        sc = getattr(self, "scope", None)
        if not sc:
            return True
        for f in sc["folders"]:
            fp, _, fpath = f.partition(":")
            if fp == pool_id and (not fpath or path == fpath or path.startswith(fpath + "/")):
                return True
        return False

    def scope_text(self, sc=None):
        sc = sc if sc is not None else getattr(self, "scope", None)
        if not sc:
            return None
        names = []
        for f in sc["folders"]:
            fp, _, fpath = f.partition(":")
            names.append(self.name_of(fp) + (" › " + fpath if fpath else ""))
        return ", ".join(names) + (" (nur untereinander)" if sc["within"] else " (mit allem verglichen)")

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
                    while self.copy_active() and not self.cancel.is_set():
                        self.progress["paused"] = True      # großer Kopiervorgang: warten
                        time.sleep(5)
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
    def start(self, trigger="manuell", folders=None, within=False):
        """folders: gezielte Suche in diesen Ordnern ("pool:pfad", pfad leer = ganzer Pool/Quelle).
        within: nur Duplikate innerhalb dieser Ordner; sonst Dateien dieser Ordner mit allem vergleichen."""
        if getattr(self, "paused_reason", None):
            raise ValueError(f"Pausiert: {self.paused_reason}")
        if self.thread and self.thread.is_alive():
            raise ValueError("Duplikatsuche läuft bereits")
        folders = [f.strip().rstrip("/") for f in (folders or []) if f and ":" in f]
        self.scope = {"folders": folders, "within": bool(within)} if folders else None
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
        p["scope"] = self.scope_text()
        with self.lock:
            cur = self.db.execute("INSERT INTO dup_runs (started, trigger, status, scope) VALUES (?,?,?,?)",
                                  (p["started"], trigger, "läuft", p["scope"]))
            run_id = cur.lastrowid
            self.db.commit()
        status, err = "fertig", None
        try:
            if cfg["refresh_index"]:
                for pool in list(self.settings.data["pools"]):
                    if self.cancel.is_set():
                        raise InterruptedError
                    # Nur geänderte Ordner neu erfassen (läuft ein Scan, wird darauf gewartet)
                    p["phase"] = f"Dateiliste {pool['name']} aktualisieren"
                    branches = [(u, b) for u, b, ok in self.pools.branches(pool) if ok]
                    self.index.refresh(pool, branches)
                p["phase"] = "Index aktualisieren"
                for src in self.sources():
                    if self.cancel.is_set():
                        raise InterruptedError
                    if src["mounted"]:
                        self.index.refresh({"id": src["id"]}, [(src["uuid"], src["root"])])
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
        active = {p["id"] for p in self.settings.data["pools"]} | {s["id"] for s in self.sources()}
        with self.lock:
            rows = [r for r in self.db.execute(
                "SELECT pool, path, size, mtime, disks FROM files WHERE size >= ? AND size IN "
                "(SELECT size FROM files WHERE size >= ? GROUP BY size HAVING count(*) > 1) "
                "ORDER BY size DESC", (min_size, min_size)).fetchall() if r[0] in active]
            cache = {(r[0], r[1]): (r[2], r[3], r[4], r[5], r[6]) for r in
                     self.db.execute("SELECT pool, path, size, mtime, partial, full, sample FROM hashes")}
        sc = getattr(self, "scope", None)
        by_size = {}
        for r in rows:
            if self._excluded(r[0], r[1], cfg):
                continue
            if sc and sc["within"] and not self._in_scope(r[0], r[1]):
                continue
            by_size.setdefault(r[2], []).append(tuple(r))
        by_size = {s: lst for s, lst in by_size.items() if len(lst) > 1}
        if sc:
            # gezielte Suche: nur Größen, bei denen mindestens eine Datei aus den gewählten Ordnern dabei ist
            by_size = {s: lst for s, lst in by_size.items() if any(self._in_scope(f[0], f[1]) for f in lst)}

        def cached(f, kind):
            c = cache.get((f[0], f[1]))
            if c and c[0] == f[2] and c[1] == f[3]:
                return {"partial": c[2], "full": c[3], "sample": c[4]}[kind]
            return None

        def store(f, partial, full, sample=None):
            old = cache.get((f[0], f[1]))
            if sample is None and old and old[0] == f[2] and old[1] == f[3]:
                sample = old[4]
            with self.lock:
                self.db.execute("INSERT OR REPLACE INTO hashes (pool, path, size, mtime, partial, full, sample) "
                                "VALUES (?,?,?,?,?,?,?)", (f[0], f[1], f[2], f[3], partial, full, sample))
                self.db.commit()   # sofort: sonst bliebe die Datenbank während langer Lesevorgänge gesperrt
            cache[(f[0], f[1])] = (f[2], f[3], partial, full, sample)

        def read(f, ranges):
            phys = self.physical_paths(f[0], f[1], f[4])
            if not phys:
                return None
            try:
                return self._read_hash(phys[0][2], ranges, cfg, pausable)
            except InterruptedError:
                raise          # Abbrechen (InterruptedError ist auch ein OSError)
            except OSError as err:
                log.debug("Nicht lesbar %s: %s", f[1], err)
                return None

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
                    if size <= 3 * PARTIAL:
                        ranges = [(0, size)]
                    else:
                        ranges = [(0, PARTIAL), (size // 2 - PARTIAL // 2, PARTIAL), (size - PARTIAL, PARTIAL)]
                    h = read(f, ranges)
                    if h is None:
                        continue
                    full = h if size <= 3 * PARTIAL else cached(f, "full")
                    store(f, h, full)
                partial_groups.setdefault((size, h), []).append(f)
            with self.lock:
                self.db.commit()

        cands = {k: v for k, v in partial_groups.items()
                 if len(v) > 1 and (not sc or any(self._in_scope(f[0], f[1]) for f in v))}

        # Schritt 3: Stichproben (~0,8 MB je Datei an 10 Stellen; kleine Dateien ganz) – trennt fast alle
        # Nicht-Duplikate, ohne die Dateien komplett zu lesen
        p["phase"] = "Stichprobenvergleich"
        p["total"] = sum(len(v) for v in cands.values())
        p["done"] = 0
        sample_groups = {}
        for (size, ph), lst in cands.items():
            for f in lst:
                p["done"] += 1
                h = cached(f, "sample")
                if h is None:
                    if size <= SAMPLE_FULL:
                        ranges = [(0, size)]
                    else:
                        step = (size - 2 * SAMPLE_EDGE) // (SAMPLE_POINTS + 1)
                        ranges = ([(0, SAMPLE_EDGE)] +
                                  [(SAMPLE_EDGE + step * (i + 1), PARTIAL) for i in range(SAMPLE_POINTS)] +
                                  [(size - SAMPLE_EDGE, SAMPLE_EDGE)])
                    h = read(f, ranges)
                    if h is None:
                        continue
                    full = h if size <= SAMPLE_FULL else cached(f, "full")
                    store(f, ph, full, h)
                sample_groups.setdefault((size, h), []).append(f)
        cands = {k: v for k, v in sample_groups.items()
                 if len(v) > 1 and (not sc or any(self._in_scope(f[0], f[1]) for f in v))}

        groups = {}
        if cfg.get("verify") == "full":
            # Schritt 4 (nur „gründlich“): vollständig lesen
            p["phase"] = "Vollvergleich"
            p["total"] = sum(len(v) for v in cands.values())
            p["total_bytes"] = sum(k[0] * len(v) for k, v in cands.items())
            p["done"] = 0
            for (size, sh), lst in cands.items():
                for f in lst:
                    p["done"] += 1
                    h = cached(f, "full")
                    if h is None:
                        h = read(f, [(0, size)])
                        if h is None:
                            continue
                        store(f, cached(f, "partial"), h, sh)
                    groups.setdefault(h, []).append(f)
                p["groups"] = sum(1 for v in groups.values() if len(v) > 1)
        else:
            # Gleiche Größe + gleiche Stichproben = Duplikat. Vor dem Löschen wird jede Datei ohnehin
            # Byte für Byte mit der bleibenden verglichen, falsche Treffer können also nichts löschen.
            for (size, sh), lst in cands.items():
                groups[f"s{size}:{sh}"] = lst
            p["groups"] = len(groups)

        p["phase"] = "Ergebnis speichern"
        out = []
        for h, lst in groups.items():
            if len(lst) > 1 and (not sc or any(self._in_scope(f[0], f[1]) for f in lst)):
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

    def _visible_counts(self):
        """(hash, size, anzahl) je Gruppe – ohne Dateien in inzwischen ausgeschlossenen Ordnern."""
        cfg = self.cfg()
        with self.lock:
            rows = self.db.execute("SELECT hash, size, pool, path FROM dup_results").fetchall()
        counts = {}
        for h, size, pool, path in rows:
            if self._excluded(pool, path, cfg):
                continue
            c = counts.setdefault(h, [size, 0])
            c[1] += 1
        return [(h, s, c) for h, (s, c) in counts.items() if c > 1]

    def summary(self):
        ign = self._ignored()
        rows = self._visible_counts()
        groups = [(h, s, c) for h, s, c in rows if h not in ign]
        return {"groups": len(groups), "files": sum(c for _, _, c in groups),
                "wasted": sum(s * (c - 1) for _, s, c in groups),
                "ignored": sum(1 for h, _, _ in rows if h in ign)}

    def _all_groups(self, search="", show_ignored=False, folder=None):
        """Alle sichtbaren Gruppen (ohne Ausgeschlossene, ohne gerade gelöschte Dateien)."""
        with self.lock:
            ign_perm = {r[0] for r in self.db.execute("SELECT hash FROM dup_ignore")}
            rows = self.db.execute("SELECT * FROM dup_results ORDER BY hash, pool, path").fetchall()
        pending = self.pending
        groups, order = {}, []
        for r in rows:
            if (r["pool"], r["path"]) in pending:
                continue
            g = groups.get(r["hash"])
            if g is None:
                g = groups[r["hash"]] = {"hash": r["hash"], "size": r["size"], "files": []}
                order.append(r["hash"])
            g["files"].append({"pool": r["pool"], "pool_name": self.name_of(r["pool"]),
                               "path": r["path"], "mtime": r["mtime"], "disks": r["disks"]})
        res = []
        s = search.lower().strip()
        cfg = self.cfg()
        for h in order:
            g = groups[h]
            g["files"] = [f for f in g["files"] if not self._excluded(f["pool"], f["path"], cfg)]
            if len(g["files"]) < 2:
                continue
            hidden = h in ign_perm or h in self.once_ignored
            if hidden != bool(show_ignored):
                continue
            if s and not any(s in f["path"].lower() for f in g["files"]):
                continue
            if folder and not any(_in_folder(f, folder) for f in g["files"]):
                continue
            names = {f["path"].rsplit("/", 1)[-1].lower() for f in g["files"]}
            g["same_name"] = len(names) == 1
            g["count"] = len(g["files"])
            g["wasted"] = g["size"] * (g["count"] - 1)
            g["ignored"] = "dauerhaft" if h in ign_perm else ("einmalig" if h in self.once_ignored else None)
            res.append(g)
        return res

    SORTS = {
        "wasted": lambda g: -g["wasted"],
        "size": lambda g: -g["size"],
        "count": lambda g: (-g["count"], -g["size"]),
        "name": lambda g: g["files"][0]["path"].rsplit("/", 1)[-1].lower(),
        "folder": lambda g: min(f["path"].rsplit("/", 1)[0].lower() for f in g["files"]),
        "newest": lambda g: -max(f["mtime"] or 0 for f in g["files"]),
        "oldest": lambda g: min(f["mtime"] or 0 for f in g["files"]),
    }

    def groups(self, offset=0, limit=50, search="", show_ignored=False, sort="wasted", folder=None):
        res = self._all_groups(search, show_ignored, folder)
        res.sort(key=self.SORTS.get(sort, self.SORTS["wasted"]))
        for g in res[offset:offset + limit]:
            g["files"].sort(key=lambda f: f["path"].lower())
        return {"total": len(res), "groups": res[offset:offset + limit]}

    # ------------------------------------------------------------ Ordneransicht und Vorschläge
    def folders(self):
        """Ordner mit Duplikaten: wie viele Dateien dort eine Kopie *außerhalb* des Ordners haben
        (die können gefahrlos gelöscht werden) – plus Vorschläge für „unsortierte“ Ordner."""
        stats = {}
        for g in self._all_groups():
            dirs = {(f["pool"], _parent(f["path"])) for f in g["files"]}
            for f in g["files"]:
                key = (f["pool"], _parent(f["path"]))
                st = stats.setdefault(key, {"pool": f["pool"], "pool_name": f["pool_name"], "path": key[1],
                                            "files": 0, "safe": 0, "bytes": 0, "inside": 0, "_other": set()})
                st["files"] += 1
                st["_other"] |= dirs - {key}
                if len(dirs) > 1:
                    st["safe"] += 1            # es gibt eine Kopie in einem anderen Ordner
                    st["bytes"] += g["size"]
                else:
                    st["inside"] += 1          # alle Kopien liegen in diesem Ordner
        for st in stats.values():
            st["spread"] = len(st.pop("_other"))   # in wie viele andere Ordner sich die Kopien verteilen
        out = sorted(stats.values(), key=lambda x: (-x["safe"], -x["bytes"]))
        # Vorschläge: Ordner, deren Duplikate fast alle auch woanders liegen; „unsortiert“-Namen bevorzugt
        sugg = []
        for st in out:
            if st["safe"] < 3:
                continue
            name = st["path"].rsplit("/", 1)[-1].lower()
            last = st["path"].rsplit("/", 1)[-1].lower()
            hint = next((w for w in UNSORTED_HINTS
                         if re.search(r"(^|[\s_\-.(])" + re.escape(w) + r"($|[\s_\-.)0-9])", last)), None)
            share = st["safe"] / max(st["files"], 1)
            score = st["safe"] * (3 if hint else 1) * share
            # Sammelordner: die Kopien seiner Dateien sind auf mehrere andere Ordner verteilt (z. B. ein
            # „Unsortiert“-Ordner, dessen Bilder in verschiedenen Alben liegen). Ein Album, dessen Dateien
            # nur in *einem* anderen Ordner doppelt sind, ist eher das Ziel als die Quelle.
            if hint or (share >= 0.8 and st["spread"] >= 2):
                sugg.append(dict(st, score=score * max(1, st["spread"]),
                                 reason=(f"Name enthält „{hint}“" if hint else
                                         f"{round(100 * share)} % der Dateien liegen sortiert in {st['spread']} anderen Ordnern"),
                                 name=name))
        sugg.sort(key=lambda x: -x["score"])
        pattern = self.select_pattern(count_only=True)
        return {"folders": out[:300], "total": len(out), "suggestions": sugg[:6], "copies": pattern}

    def select_folder(self, pool, folder, mode="mark", sub=True):
        """Auswahl für einen Ordner über *alle* Gruppen.
        mode=mark: Dateien in diesem Ordner markieren (nur, wo eine Kopie außerhalb bleibt).
        mode=keep: diesen Ordner behalten – die Kopien in anderen Ordnern markieren."""
        target = {"pool": pool, "path": folder.strip("/"), "sub": sub}
        items = []
        for g in self._all_groups():
            inside = [f for f in g["files"] if _in_folder(f, target)]
            outside = [f for f in g["files"] if not _in_folder(f, target)]
            if not inside or not outside:
                continue
            pick = inside if mode == "mark" else outside
            items += [{"hash": g["hash"], "size": g["size"], "count": len(g["files"]), "pool": f["pool"], "path": f["path"]} for f in pick]
        return {"items": items}

    def select_pattern(self, count_only=False):
        """Mustererkennung: Dateien wie „Foto (1).jpg“, „Foto - Kopie.jpg“, „Foto copy 2.jpg“ markieren,
        wenn in der Gruppe eine Datei ohne dieses Muster bleibt."""
        items = []
        for g in self._all_groups():
            copies = [f for f in g["files"] if COPY_RE.search(f["path"].rsplit("/", 1)[-1])]
            if copies and len(copies) < len(g["files"]):
                items += [{"hash": g["hash"], "size": g["size"], "count": len(g["files"]), "pool": f["pool"], "path": f["path"]} for f in copies]
        if count_only:
            return {"files": len(items), "bytes": sum(i["size"] for i in items)}
        return {"items": items}

    def ignore(self, hashes, kind, note=""):
        if kind == "once":
            self.once_ignored.update(hashes)
        elif kind == "forever":
            with self.lock:
                rows = []
                for h in hashes:
                    files = self.db.execute("SELECT pool, path, size FROM dup_results WHERE hash=? ORDER BY path",
                                            (h,)).fetchall()
                    info = {"note": note, "size": files[0][2] if files else None,
                            "files": [{"pool": f[0], "path": f[1]} for f in files]}
                    rows.append((h, "dauerhaft", int(time.time()), json.dumps(info, ensure_ascii=False)))
                self.db.executemany("INSERT OR REPLACE INTO dup_ignore VALUES (?,?,?,?)", rows)
                self.db.commit()
            self.once_ignored.difference_update(hashes)
        elif kind == "unignore":
            self.once_ignored.difference_update(hashes)
            with self.lock:
                self.db.executemany("DELETE FROM dup_ignore WHERE hash=?", [(h,) for h in hashes])
                self.db.commit()

    def ignored_list(self):
        """Dauerhaft ignorierte Gruppen inkl. der Dateien zum Zeitpunkt des Ignorierens."""
        with self.lock:
            rows = self.db.execute("SELECT hash, created, note FROM dup_ignore ORDER BY created DESC").fetchall()
            current = {}
            for h, pool, path in self.db.execute(
                    "SELECT hash, pool, path FROM dup_results WHERE hash IN (SELECT hash FROM dup_ignore)"):
                current.setdefault(h, []).append({"pool": pool, "path": path})
        out = []
        for h, created, note in rows:
            try:
                info = json.loads(note or "{}")
            except json.JSONDecodeError:
                info = {}
            files = current.get(h) or info.get("files") or []
            for f in files:
                f["pool_name"] = self.name_of(f["pool"])
            out.append({"hash": h, "created": created, "size": info.get("size"), "files": files,
                        "still_present": h in current})
        return out

    def browse(self, pool_id, path):
        """Unterordner für die Ordnerauswahl (aus dem Datei-Index)."""
        if not pool_id:
            return {"pool": None, "path": "", "folders": [
                {"name": p["name"], "pool": p["id"], "path": ""} for p in self.settings.data["pools"]] + [
                {"name": s["name"], "pool": s["id"], "path": "", "source": True, "network": s["network"]}
                for s in self.sources()]}
        with self.lock:
            rows = self.db.execute("SELECT path, name, files, bytes FROM folders WHERE pool=? AND parent=? "
                                   "ORDER BY name COLLATE NOCASE", (pool_id, path)).fetchall()
        return {"pool": pool_id, "pool_name": self.name_of(pool_id), "path": path,
                "folders": [{"name": r[1], "pool": pool_id, "path": r[0], "files": r[2], "bytes": r[3]} for r in rows]}

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

    def delete_async(self, items):
        """Löschen im Hintergrund über eine Warteschlange: die Dateien verschwinden sofort aus der Liste
        (pending); weitere Löschaufträge werden angehängt, auch während schon gelöscht wird."""
        if self.options["read_only"]:
            raise ValueError("Schreibschutz aktiv – zum Löschen 'read_only' in den Add-on-Optionen ausschalten")
        items = [i for i in items if (i["pool"], i["path"]) not in self.pending]
        keys = {(i["pool"], i["path"]) for i in items}
        if not keys:
            return {"ok": True, "total": 0}
        with self.del_lock:
            self.pending |= keys
            self.del_queue.append((items, keys))
            j = self.del_job
            if j.get("running"):
                j["total"] = j.get("total", 0) + len(keys)
            else:
                self.del_job = {"running": True, "total": len(keys), "done": 0, "deleted": 0, "freed": 0,
                                "errors": [], "started": int(time.time())}
                threading.Thread(target=self._delete_worker, daemon=True, name="dupes-delete").start()
        return {"ok": True, "total": len(keys), "queued": self.del_job["total"] - self.del_job["done"]}

    def _delete_worker(self):
        job = self.del_job
        while True:
            with self.del_lock:
                if not self.del_queue:
                    job["running"] = False
                    job["finished"] = int(time.time())
                    return
                items, keys = self.del_queue.pop(0)
            try:
                base = {k: job[k] for k in ("done", "deleted", "freed")}

                class _P(dict):   # Fortschritt des Teilauftrags in den Gesamtauftrag spiegeln
                    def __setitem__(s, k, v):
                        dict.__setitem__(s, k, v)
                        if k in base:
                            job[k] = base[k] + v
                r = self.delete(items, progress=_P())
                job["deleted"] = base["deleted"] + r["deleted"]
                job["freed"] = base["freed"] + r["freed"]
                job["errors"] += r["errors"]
                job["done"] = base["done"] + len(keys)
            except Exception as err:  # noqa: BLE001
                job["errors"].append(str(err))
                job["done"] = base["done"] + len(keys)
            finally:
                with self.del_lock:
                    self.pending -= keys

    def delete(self, items, progress=None):
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
                if progress is not None:
                    progress["done"] = progress.get("done", 0) + 1
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
                if progress is not None:
                    progress["deleted"], progress["freed"] = results["deleted"], results["freed"]
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
        for branch in self.roots():
            for root, _, files in os.walk(os.path.join(branch, ".nas-trash")):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                        count += 1
                    except OSError:
                        pass
        return {"bytes": total, "files": count}

    def trash_list(self):
        """Papierkorb nach Löschvorgang (Zeitstempel) gruppiert, mit Dateiliste (gekürzt)."""
        batches = {}
        for branch in self.roots():
            t = os.path.join(branch, ".nas-trash")
            if not os.path.isdir(t):
                continue
            for stamp in os.listdir(t):
                b = batches.setdefault(stamp, {"stamp": stamp, "files": 0, "bytes": 0, "sample": []})
                base = os.path.join(t, stamp)
                for root, _, names in os.walk(base):
                    for n in names:
                        p = os.path.join(root, n)
                        try:
                            b["bytes"] += os.path.getsize(p)
                        except OSError:
                            continue
                        b["files"] += 1
                        rel = os.path.relpath(p, base)
                        if len(b["sample"]) < 300 and rel not in b["sample"]:
                            b["sample"].append(rel)
        out = []
        for b in sorted(batches.values(), key=lambda x: x["stamp"], reverse=True):
            try:
                b["time"] = int(dt.datetime.strptime(b["stamp"], "%Y%m%d-%H%M%S").timestamp())
            except ValueError:
                b["time"] = None
            out.append(b)
        return {"batches": out, "files": sum(b["files"] for b in out), "bytes": sum(b["bytes"] for b in out)}

    def trash_action(self, stamp, action, paths=None):
        """action=purge: endgültig löschen; action=restore: an den alten Platz zurück.
        paths: nur diese Dateien (relativ), sonst den ganzen Löschvorgang."""
        if self.options["read_only"]:
            raise ValueError("Schreibschutz aktiv")
        if not re.fullmatch(r"\d{8}-\d{6}", stamp or ""):
            raise ValueError("Ungültiger Eintrag")
        done, errors = 0, []
        for branch in self.roots():
            base = os.path.join(branch, ".nas-trash", stamp)
            if not os.path.isdir(base):
                continue
            targets = [os.path.join(base, p) for p in paths] if paths else [base]
            for tgt in targets:
                if not os.path.realpath(tgt).startswith(os.path.realpath(base)):
                    continue
                if action == "purge":
                    if os.path.isdir(tgt):
                        shutil.rmtree(tgt, ignore_errors=True)
                    elif os.path.exists(tgt):
                        os.remove(tgt)
                    done += 1
                elif action == "restore":
                    walk = [(r, n) for r, _, ns in os.walk(tgt) for n in ns] if os.path.isdir(tgt) else \
                        [(os.path.dirname(tgt), os.path.basename(tgt))]
                    for r, n in walk:
                        src = os.path.join(r, n)
                        rel = os.path.relpath(src, base)
                        dest = os.path.join(branch, rel)
                        if os.path.exists(dest):
                            errors.append(f"Existiert schon: {rel}")
                            continue
                        try:
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            os.rename(src, dest)
                            done += 1
                        except OSError as err:
                            errors.append(f"{rel}: {err}")
            # leere Ordner im Papierkorb aufräumen
            for r, ds, ns in sorted(os.walk(base), key=lambda x: -len(x[0])):
                if not ds and not ns:
                    try:
                        os.rmdir(r)
                    except OSError:
                        pass
        return {"done": done, "errors": errors}

    def purge_trash(self, older_than_days=None):
        if self.options["read_only"]:
            return
        days = self.cfg()["trash_days"] if older_than_days is None else older_than_days
        limit = dt.datetime.now() - dt.timedelta(days=days)
        for branch in self.roots():
            t = os.path.join(branch, ".nas-trash")
            if not os.path.isdir(t):
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
        if p.get("running") and str(p.get("phase", "")).startswith("Dateiliste"):
            prog = [x for x in self.index.progress.values() if x.get("running")]
            if prog:
                p["phase"] += f" – {sum(x['files'] for x in prog):,} Dateien erfasst".replace(",", ".")
        return {"progress": p, "last": self.last_run(), "summary": self.summary(), "config": self.cfg(),
                "idle": {"cpu": round(self.idle.cpu, 1), "io_mb_s": round(self.idle.io, 2),
                         "idle_for": int(self.idle.idle_for())},
                "read_only": self.options["read_only"], "sources": self.sources(), "delete_job": self.del_job}
