"""Datei-Index: auf welchen Platten liegt welche Datei wie oft (Duplizierung)."""
import logging
import os
import sqlite3
import threading
import time

from . import config

log = logging.getLogger("nas")

SKIP_NAMES = {".covefs", "$RECYCLE.BIN", "System Volume Information", ".nas-trash", ".nas"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  pool TEXT NOT NULL, path TEXT NOT NULL, size INTEGER, mtime INTEGER,
  copies INTEGER, disks TEXT, consistent INTEGER,
  PRIMARY KEY (pool, path)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS folders (
  pool TEXT NOT NULL, path TEXT NOT NULL, parent TEXT, name TEXT,
  files INTEGER, bytes INTEGER, stored INTEGER, min_c INTEGER, max_c INTEGER,
  c1 INTEGER, c2 INTEGER, c3 INTEGER, inconsistent INTEGER, under INTEGER, target INTEGER,
  PRIMARY KEY (pool, path)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS folders_parent ON folders(pool, parent);
CREATE TABLE IF NOT EXISTS scans (
  pool TEXT PRIMARY KEY, started INTEGER, finished INTEGER, files INTEGER, bytes INTEGER,
  status TEXT, error TEXT);
"""


def connect():
    os.makedirs(os.path.dirname(config.INDEX_DB), exist_ok=True)
    db = sqlite3.connect(config.INDEX_DB, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    return db


def parent_of(path):
    return path.rsplit("/", 1)[0] if "/" in path else ""


def effective_target(path, targets, default):
    """Soll-Duplizierung: nächster Elternordner mit Einstellung gewinnt."""
    p = path
    while True:
        if p in targets:
            return targets[p]
        if not p:
            return default
        p = parent_of(p)


class Index:
    stale = []   # gemeinsame Liste gefundener Temp-Reste (wird von der Replikation aufgeräumt)

    def __init__(self):
        self.lock = threading.Lock()
        self._quick = {}
        self.db = connect()
        self.progress = {}   # pool -> laufender Scan-Status

    # ------------------------------------------------------------ Scan
    def scan(self, pool, branches):
        """branches: Liste (disk_uuid, pfad). Läuft im Hintergrund-Thread."""
        pid = pool["id"]
        prog = {"running": True, "started": int(time.time()), "files": 0, "bytes": 0,
                "disk": None, "disks_done": 0, "disks_total": len(branches), "error": None}
        self.progress[pid] = prog
        with self.lock:
            self.db.execute("DELETE FROM files WHERE pool=?", (pid,))
            self.db.execute("INSERT OR REPLACE INTO scans VALUES (?,?,?,?,?,?,?)",
                            (pid, prog["started"], None, 0, 0, "läuft", None))
            self.db.commit()
        upsert = ("INSERT INTO files VALUES (?,?,?,?,1,?,1) ON CONFLICT(pool,path) DO UPDATE SET "
                  "copies=copies+1, disks=disks||','||excluded.disks, "
                  "consistent=consistent AND size=excluded.size, mtime=max(mtime, excluded.mtime)")
        try:
            for disk_uuid, root in branches:
                prog["disk"] = disk_uuid
                batch = []
                for rel, st in self._walk(root):
                    batch.append((pid, rel, st.st_size, int(st.st_mtime), disk_uuid))
                    prog["files"] += 1
                    prog["bytes"] += st.st_size
                    if len(batch) >= 5000:
                        self._flush(upsert, batch)
                        batch = []
                self._flush(upsert, batch)
                prog["disks_done"] += 1
            self.aggregate(pool)
            status, err = "fertig", None
        except Exception as e:  # noqa: BLE001
            log.exception("Scan von Pool %s fehlgeschlagen", pid)
            status, err = "fehler", str(e)
            prog["error"] = err
        finally:
            prog["running"] = False
            prog["finished"] = int(time.time())
            with self.lock:
                self.db.execute("UPDATE scans SET finished=?, files=?, bytes=?, status=?, error=? WHERE pool=?",
                                (prog["finished"], prog["files"], prog["bytes"], status, err, pid))
                self.db.commit()
        log.info("Scan %s: %s Dateien, Status %s", pid, prog["files"], status)

    def _flush(self, sql, batch):
        if not batch:
            return
        with self.lock:
            self.db.executemany(sql, batch)
            self.db.commit()

    @staticmethod
    def _walk(root):
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            full = os.path.join(root, rel_dir) if rel_dir else root
            try:
                with os.scandir(full) as it:
                    for e in it:
                        if (not rel_dir and e.name in SKIP_NAMES) or e.name == ".nas-recycle":
                            continue
                        rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                        if e.name.startswith(".nas-tmp-"):
                            Index.stale.append(os.path.join(full, e.name))
                            continue
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(rel)
                            elif e.is_file(follow_symlinks=False):
                                yield rel, e.stat(follow_symlinks=False)
                        except OSError as err:
                            log.warning("Übersprungen %s: %s", rel, err)
            except OSError as err:
                log.warning("Ordner nicht lesbar %s: %s", full, err)

    # ------------------------------------------------------------ Auswertung
    def aggregate(self, pool):
        """Ordnerstatistik (rekursiv) inkl. Soll/Ist-Vergleich neu berechnen."""
        pid = pool["id"]
        targets = {k: int(v) for k, v in (pool.get("targets") or {}).items()}
        default = int(pool.get("default_target", 1))
        folders = {}

        def node(path):
            f = folders.get(path)
            if f is None:
                f = folders[path] = [0, 0, 0, 99, 0, 0, 0, 0, 0, 0]
                # files, bytes, stored, min, max, c1, c2, c3, inconsistent, under
            return f

        node("")
        target_cache = {}
        with self.lock:
            rows = self.db.execute("SELECT path,size,copies,consistent FROM files WHERE pool=?", (pid,)).fetchall()
        for path, size, copies, consistent in rows:
            folder = parent_of(path)
            t = target_cache.get(folder)
            if t is None:
                # Soll kann nie höher sein als die Anzahl der Platten im Pool
                t = target_cache[folder] = min(effective_target(folder, targets, default), max(len(pool["members"]), 1))
            under = copies < t
            p = folder
            while True:
                f = node(p)
                f[0] += 1
                f[1] += size
                f[2] += size * copies
                f[3] = min(f[3], copies)
                f[4] = max(f[4], copies)
                f[5 + min(copies, 3) - 1] += 1
                if not consistent:
                    f[8] += 1
                if under:
                    f[9] += 1
                if not p:
                    break
                p = parent_of(p)
        out = []
        for path, f in folders.items():
            out.append((pid, path, parent_of(path) if path else None, path.rsplit("/", 1)[-1],
                        f[0], f[1], f[2], f[3] if f[0] else 0, f[4], f[5], f[6], f[7], f[8], f[9],
                        targets.get(path)))
        with self.lock:
            self.db.execute("DELETE FROM folders WHERE pool=?", (pid,))
            self.db.executemany("INSERT INTO folders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
            self.db.commit()

    def folder(self, pid, path):
        with self.lock:
            r = self.db.execute("SELECT * FROM folders WHERE pool=? AND path=?", (pid, path)).fetchone()
        return dict(r) if r else None

    def folder_quick(self, pid, path=""):
        """Wie folder(), wartet aber höchstens kurz – während Scans/Abgleichen bleibt die Oberfläche bedienbar."""
        key = (pid, path)
        if self.lock.acquire(timeout=1.5):
            try:
                r = self.db.execute("SELECT * FROM folders WHERE pool=? AND path=?", (pid, path)).fetchone()
            finally:
                self.lock.release()
            self._quick[key] = dict(r) if r else None
        return self._quick.get(key)

    def children(self, pid, path):
        with self.lock:
            rows = self.db.execute("SELECT * FROM folders WHERE pool=? AND parent=? ORDER BY name COLLATE NOCASE",
                                   (pid, path)).fetchall()
        return [dict(r) for r in rows]

    def files(self, pid, path, mode="direct", limit=500):
        """mode: direct = Dateien direkt im Ordner; under/inconsistent/single = rekursiv gefiltert."""
        prefix = f"{path}/" if path else ""
        sql = "SELECT path,size,mtime,copies,disks,consistent FROM files WHERE pool=?"
        args = [pid]
        if prefix:
            sql += " AND path >= ? AND path < ?"
            args += [prefix, prefix + "￿"]
        if mode == "inconsistent":
            sql += " AND consistent=0"
        elif mode == "single":
            sql += " AND copies=1"
        elif mode == "multi":
            sql += " AND copies>1"
        with self.lock:
            rows = self.db.execute(sql + " ORDER BY path LIMIT ?", args + [limit * 20 if mode == "direct" else limit]).fetchall()
        res = []
        for r in rows:
            rel = r["path"][len(prefix):]
            if mode == "direct" and "/" in rel:
                continue
            res.append(dict(r))
            if len(res) >= limit:
                break
        return res

    def under_files(self, pool, path, limit=500):
        targets = {k: int(v) for k, v in (pool.get("targets") or {}).items()}
        default = int(pool.get("default_target", 1))
        res = []
        for f in self.files(pool["id"], path, mode="all", limit=1_000_000):
            t = min(effective_target(parent_of(f["path"]), targets, default), max(len(pool["members"]), 1))
            if f["copies"] < t:
                f["target"] = t
                res.append(f)
                if len(res) >= limit:
                    break
        return res

    def scan_info(self, pid):
        prog = self.progress.get(pid)
        if prog and prog["running"]:
            return dict(prog, status="läuft")
        if self.lock.acquire(timeout=1.5):
            try:
                r = self.db.execute("SELECT * FROM scans WHERE pool=?", (pid,)).fetchone()
            finally:
                self.lock.release()
            self._quick[("scan", pid)] = dict(r) if r else None
        return self._quick.get(("scan", pid))

    def suggest_targets(self, pid):
        """Erkannte DrivePool-Duplizierung in minimale Soll-Einstellungen umrechnen."""
        targets = {}

        def mode_of(f):
            counts = {1: f["c1"], 2: f["c2"], 3: f["c3"]}
            return max(counts, key=lambda k: (counts[k], k))

        def walk(path, inherited):
            f = self.folder(pid, path)
            if not f or not f["files"]:
                return
            m = mode_of(f)
            if f["min_c"] == f["max_c"]:
                if f["min_c"] != inherited:
                    targets[path] = min(f["min_c"], 3)
                return
            current = inherited
            if m != inherited:
                targets[path] = m
                current = m
            for ch in self.children(pid, path):
                walk(ch["path"], current)

        walk("", 1)
        return targets

    # ------------------------------------------------------------ Einzelne Dateien (Replikation)
    def upsert_file(self, pid, path, size, mtime, disks, consistent=True):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?)",
                            (pid, path, size, mtime, len(disks), ",".join(disks), 1 if consistent else 0))
            self.db.commit()

    def delete_file(self, pid, path):
        with self.lock:
            self.db.execute("DELETE FROM files WHERE pool=? AND path=?", (pid, path))
            self.db.commit()

    def delete_prefix(self, pid, prefix):
        with self.lock:
            self.db.execute("DELETE FROM files WHERE pool=? AND path >= ? AND path < ?",
                            (pid, prefix + "/", prefix + "/\uffff"))
            self.db.commit()

    def needs_fix(self, pool, remove_excess=True, members=None):
        """Pfade, deren Kopienanzahl nicht zum Soll passt oder deren Kopien abweichen."""
        pid = pool["id"]
        targets = {k: int(v) for k, v in (pool.get("targets") or {}).items()}
        default = int(pool.get("default_target", 1))
        n = members or len(pool["members"])
        cache = {}
        out = []
        with self.lock:
            rows = self.db.execute("SELECT path, copies, consistent FROM files WHERE pool=?", (pid,)).fetchall()
        for path, copies, consistent in rows:
            folder = parent_of(path)
            t = cache.get(folder)
            if t is None:
                t = cache[folder] = min(effective_target(folder, targets, default), n)
            if copies < t or (remove_excess and copies > t) or not consistent:
                out.append(path)
        return out

    def balance_candidates(self, pid, src, dst, limit=200):
        with self.lock:
            rows = self.db.execute("SELECT path, size, disks FROM files WHERE pool=? AND size > 0 "
                                   "ORDER BY size DESC LIMIT 20000", (pid,)).fetchall()
        res = []
        for path, size, disks in rows:
            d = disks.split(",")
            if src in d and dst not in d:
                res.append((path, size))
                if len(res) >= limit:
                    break
        return res

    def move_disk(self, pid, path, src, dst):
        with self.lock:
            r = self.db.execute("SELECT disks FROM files WHERE pool=? AND path=?", (pid, path)).fetchone()
            if r:
                d = [dst if x == src else x for x in r[0].split(",")]
                self.db.execute("UPDATE files SET disks=? WHERE pool=? AND path=?", (",".join(d), pid, path))
                self.db.commit()

    def files_on_disk(self, pid, uuid):
        with self.lock:
            rows = self.db.execute("SELECT path, size, disks FROM files WHERE pool=?", (pid,)).fetchall()
        return [(p, s, d) for p, s, d in rows if uuid in d.split(",")]

    def delete_pool(self, pid):
        with self.lock:
            for t in ("files", "folders", "scans"):
                self.db.execute(f"DELETE FROM {t} WHERE pool=?", (pid,))
            self.db.commit()
