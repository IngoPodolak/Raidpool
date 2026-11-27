"""Live-Ansicht: was passiert gerade (Kopieren, Cache, Scans, Duplikatsuche), Auslastung von CPU,
Speicher und Platten sowie über die Freigaben geöffnete und zuletzt geänderte Dateien."""
import json
import os
import subprocess
import threading
import time

from . import config


class LiveMonitor:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self._prev = None
        self._smb = (0, [])
        self.data = {"cpu": 0.0, "mem": 0.0, "self_cpu": 0.0, "disks": {}}
        self.speed = {}          # Kopiergeschwindigkeit je Auftrag
        self._last_copy = None
        self._copy_seen = 0.0     # zuletzt ein großer Kopiervorgang über die Freigaben erkannt

    # ------------------------------------------------------------ Messen
    @staticmethod
    def _read():
        with open("/proc/stat", encoding="utf-8") as fh:
            v = list(map(int, fh.readline().split()[1:]))
        idle, total = v[3] + (v[4] if len(v) > 4 else 0), sum(v[:8])
        disks = {}
        try:
            with open("/proc/diskstats", encoding="utf-8") as fh:
                for line in fh:
                    p = line.split()
                    n = p[2]
                    if (n[:2] in ("sd", "vd", "hd") and not n[-1].isdigit()) or \
                            (n.startswith("nvme") and "p" not in n[4:]) or n.startswith("md"):
                        disks[n] = (int(p[5]) * 512, int(p[9]) * 512, int(p[12]))   # gelesen, geschrieben, ms aktiv
        except OSError:
            pass
        try:
            with open("/proc/self/stat", encoding="utf-8") as fh:
                f = fh.read().rsplit(")", 1)[1].split()
            own = int(f[11]) + int(f[12])          # utime + stime (Ticks)
        except (OSError, IndexError, ValueError):
            own = 0
        procs = {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                    raw = fh.read()
                name = raw[raw.index("(") + 1:raw.rindex(")")]
                f = raw.rsplit(")", 1)[1].split()
                procs[pid] = (name, int(f[11]) + int(f[12]))
            except (OSError, ValueError, IndexError):
                continue
        return time.time(), idle, total, disks, own, procs

    def loop(self):
        tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        ncpu = os.cpu_count() or 1
        while True:
            try:
                cur = self._read()
                if self._prev:
                    t0, i0, tt0, d0, o0, p0 = self._prev
                    t1, i1, tt1, d1, o1, p1 = cur
                    dt = max(t1 - t0, 0.1)
                    cpu = 100 * (1 - (i1 - i0) / max(tt1 - tt0, 1))
                    disks = {}
                    for n, (r, w, ms) in d1.items():
                        if n in d0:
                            r0, w0, ms0 = d0[n]
                            disks[n] = {"read": (r - r0) / dt, "write": (w - w0) / dt,
                                        "busy": min(100.0, (ms - ms0) / (dt * 10))}
                    mem = 0.0
                    try:
                        mi = {}
                        with open("/proc/meminfo", encoding="utf-8") as fh:
                            for line in fh:
                                k, v = line.split(":", 1)
                                mi[k] = int(v.split()[0])
                        mem = 100 * (1 - mi.get("MemAvailable", 0) / max(mi.get("MemTotal", 1), 1))
                    except OSError:
                        pass
                    self_cpu = 100 * (o1 - o0) / tick / dt / ncpu
                    # CPU je Programm im Add-on (Samba, mergerfs, NTFS-Treiber, RAIDPool) – in % eines Kerns
                    byname = {}
                    for pid, (name, ticks) in p1.items():
                        if pid in p0 and p0[pid][0] == name:
                            byname[name] = byname.get(name, 0) + (ticks - p0[pid][1])
                    procs = sorted(({"name": n, "cpu": round(100 * v / tick / dt, 1)} for n, v in byname.items()
                                    if v > 0), key=lambda x: -x["cpu"])[:6]
                    with self.lock:
                        self.data = {"cpu": round(cpu, 1), "mem": round(mem, 1), "cores": ncpu,
                                     "self_cpu": round(self_cpu, 1), "disks": disks, "procs": procs}
                    self._copy_speed(dt)
                    self._detect_copy(disks)
                self._prev = cur
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)

    def _detect_copy(self, disks):
        """Großer Kopiervorgang = über die Freigaben ist eine Datei zum Schreiben geöffnet und es wird
        mit mehr als 20 MB/s geschrieben. Hält 30 s nach, damit kurze Pausen nichts anwerfen."""
        writing = any(f["write"] for f in self._smb_open())
        rate = sum(v["write"] for v in disks.values())
        if writing and rate > 20e6:
            self._copy_seen = time.time()

    def mover_active(self):
        return any((st or {}).get("running") for st in list(self.app.cache.state.values()))

    def copy_active(self):
        """Großer Kopiervorgang über die Freigaben oder der SSD-Cache wird gerade auf die Platten verteilt."""
        return time.time() - self._copy_seen < 30 or self.mover_active()

    def _copy_speed(self, dt):
        c = self.app.rep.current
        key = (c["pool"], c["path"]) if c else None
        done = c.get("done", 0) if c else 0
        if key and self._last_copy and self._last_copy[0] == key:
            self.speed["rep"] = max(0.0, (done - self._last_copy[1]) / dt)
        else:
            self.speed["rep"] = 0.0
        self._last_copy = (key, done) if key else None

    def _smb_open(self):
        """Über die Freigaben geöffnete Dateien (smbstatus), höchstens alle 3 s abgefragt."""
        t, cached = self._smb
        if time.time() - t < 3 or config.DEV:
            return cached
        res = []
        try:
            out = subprocess.run(["smbstatus", "-L", "--json"], capture_output=True, text=True, timeout=5).stdout
            data = json.loads(out or "{}")
            for f in (data.get("open_files") or {}).values():
                opens = list((f.get("opens") or {}).values())
                write = any("WRITE" in str((o.get("access_mask") or {}).get("text", "")).upper()
                            or (o.get("access_mask") or {}).get("WRITE_DATA") for o in opens)
                res.append({"path": os.path.join(f.get("service_path", ""), f.get("filename", "")),
                            "write": bool(write), "clients": len(opens)})
        except Exception:  # noqa: BLE001
            pass
        res.sort(key=lambda x: (not x["write"], x["path"]))
        self._smb = (time.time(), res)
        return res

    # ------------------------------------------------------------ Ausgabe
    def disk_names(self):
        names = {}
        for d in self.app.disks.disks:
            dev = os.path.basename(d.get("path") or "")
            label = next((p.get("label") for p in d.get("partitions", []) if p.get("label")), None)
            names[dev] = {"model": d.get("model") or dev, "label": label, "system": d.get("system", False)}
        return names

    def snapshot(self):
        app = self.app
        with self.lock:
            sysd = dict(self.data)
        names = self.disk_names()
        disks = []
        for dev, v in sorted(sysd.pop("disks", {}).items()):
            n = names.get(dev, {"model": dev, "label": None, "system": False})
            disks.append({"dev": dev, **n, "read": v["read"], "write": v["write"], "busy": round(v["busy"], 1)})
        rep = app.rep
        job = dict(rep.job) if rep.job else None
        if job:
            job.pop("kw", None)
            if not job.get("running"):
                job = None
        cache = []
        for pool in list(app.settings.data["pools"]):
            st = app.cache.state.get(pool["id"]) or {}
            if st.get("running"):
                cache.append({"pool": pool["name"], "current": st.get("current"), "active": list(st.get("active") or []),
                              "done": st.get("done", 0),
                              "total": st.get("total", 0), "bytes": st.get("moved_bytes", 0)})
        scans = []
        for pid, pr in list(app.index.progress.items()):
            if pr.get("running"):
                pool = app.settings.pool(pid)
                scans.append({"pool": pool["name"] if pool else pid, "files": pr.get("files", 0),
                              "dirs": pr.get("dirs"), "quick": pr.get("quick", False),
                              "disks_done": pr.get("disks_done", 0), "disks_total": pr.get("disks_total", 0)})
        dp = app.dupes.progress or {}
        dupes = {k: dp.get(k) for k in ("phase", "done", "total", "read_bytes", "paused", "groups")} \
            if dp.get("running") else None
        health = app.health.check if app.health.check and app.health.check.get("running") else None
        audit = getattr(app, "audit", None)
        return {
            "t": int(time.time()),
            "copy_active": self.copy_active(),
            "system": sysd,
            "disks": disks,
            "rep": {"current": rep.current, "speed": self.speed.get("rep", 0.0), "queue": len(rep.queue),
                    "job": job, "stats": rep.stats, "activity": list(rep.activity)[:6]},
            "cache": cache,
            "scans": scans,
            "scan_paused": app.index.paused if hasattr(app.index, "paused") else False,
            "dupes": dupes,
            "check": {k: health.get(k) for k in ("label", "tool", "phase")} if health else None,
            "smb_open": self._smb_open()[:12],
            "recent": list(audit.recent)[:12] if audit else [],
        }

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="live").start()
