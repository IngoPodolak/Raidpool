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
}


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
        for r, _, names in os.walk(root):
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
        for age_ts, size, rel in files:
            age = now - age_ts
            if age < 60:
                continue                   # gerade erst geschrieben
            if mode == "all" or (c["move_after_min"] and age >= c["move_after_min"] * 60) or \
                    (pressure and remaining > target_bytes):
                todo.append((rel, size))
                remaining -= size
        if not todo:
            return
        open_files = self._open_files()
        st.update(running=True, started=int(time.time()), total=len(todo), done=0, moved_bytes=0)
        for rel, size in todo:
            if self.stop.is_set() or self.paused_reason:
                break
            if os.path.join(root, rel) in open_files:
                continue
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
