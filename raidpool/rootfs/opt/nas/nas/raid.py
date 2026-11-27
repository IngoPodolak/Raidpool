"""RAID-Verbünde mit mdadm (Linux-Software-RAID): RAID 0, 1, 5, 6, 10 und JBOD (linear).

Sicherheit: Es werden nur Platten angeboten, die nicht zum System gehören, keinem Pool angehören,
nicht eingehängt sind und keinen DrivePool-Ordner enthalten. Vor dem Anlegen muss der Name des
Verbunds eingetippt werden – alle Daten auf den gewählten Platten werden gelöscht.
"""
import logging
import os
import re
import threading
import time

from . import config
from .util import run

log = logging.getLogger("nas")

LEVELS = {
    "raid0": {"label": "RAID 0 (Stripe – schnell, keine Ausfallsicherheit)", "min": 2, "parity": 0},
    "raid1": {"label": "RAID 1 (Spiegel)", "min": 2, "parity": None},
    "raid5": {"label": "RAID 5 (1 Platte darf ausfallen)", "min": 3, "parity": 1},
    "raid6": {"label": "RAID 6 (2 Platten dürfen ausfallen)", "min": 4, "parity": 2},
    "raid10": {"label": "RAID 10 (Spiegel + Stripe)", "min": 4, "parity": None},
    "linear": {"label": "JBOD (Platten hintereinander, keine Ausfallsicherheit)", "min": 2, "parity": 0},
}
FILESYSTEMS = ("ext4", "xfs")
MDCONF = config.DATA_DIR + "/mdadm.conf"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


def capacity(level, sizes):
    if not sizes:
        return 0
    n, small = len(sizes), min(sizes)
    if level in ("raid0", "linear"):
        return sum(sizes) if level == "linear" else small * n
    if level == "raid1":
        return small
    if level == "raid5":
        return small * (n - 1)
    if level == "raid6":
        return small * (n - 2)
    if level == "raid10":
        return small * n // 2
    return 0


class RaidManager:
    def __init__(self, options, settings, disks):
        self.options = options
        self.settings = settings
        self.disks = disks
        self.job = None
        self.lock = threading.Lock()
        self.support = None

    # ------------------------------------------------------------ Unterstützung im Kernel
    def check_support(self):
        if config.DEV:
            self.support = {"available": True, "levels": list(LEVELS), "mdadm": True}
            return self.support
        for mod in ("md_mod", "raid0", "raid1", "raid456", "raid10", "linear", "md_linear"):
            run(["modprobe", mod], timeout=20)
        levels = []
        try:
            with open("/proc/mdstat", encoding="utf-8") as fh:
                first = fh.readline()
            m = re.search(r"Personalities\s*:\s*(.*)", first)
            if m:
                levels = [p.strip("[]") for p in m.group(1).split()]
            available = True
        except OSError:
            available = False
        mdadm = run(["mdadm", "--version"], timeout=10)[0] != 127
        self.support = {"available": available and mdadm, "levels": levels, "mdadm": mdadm,
                        "reason": None if available else "Der Linux-Kern dieses Systems unterstützt kein Software-RAID (md)"}
        return self.support

    # ------------------------------------------------------------ Zusammensetzen beim Start
    def assemble(self):
        if config.DEV or not (self.support or {}).get("available"):
            return
        args = ["mdadm", "--assemble", "--scan", "--run"]
        if os.path.exists(MDCONF):
            args += ["--config", MDCONF]
        run(args, timeout=120)
        self.save_conf()

    def save_conf(self):
        if config.DEV:
            return
        code, out, _ = run(["mdadm", "--detail", "--scan"], timeout=30)
        if code == 0:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(MDCONF, "w", encoding="utf-8") as fh:
                fh.write("DEVICE partitions\n" + out)

    # ------------------------------------------------------------ Status
    def _dev_arrays(self):
        """Simulierte Verbünde im Entwicklungsmodus."""
        out = []
        for a in self.settings.data.get("dev_raids", []):
            prog = min(100.0, (time.time() - a["created"]) * 4)
            out.append({"name": a["name"], "device": f"/dev/md/{a['name']}", "md": "md127", "level": a["level"],
                        "state": "clean" if prog >= 100 else "active", "degraded": 0, "raid_disks": len(a["members"]),
                        "size": a["size"], "sync_action": "idle" if prog >= 100 else "resync",
                        "sync_progress": None if prog >= 100 else round(prog, 1), "members": [
                            {"device": m, "state": "in_sync", "slot": i} for i, m in enumerate(a["members"])],
                        "uuid": a.get("uuid"), "filesystem": a.get("fs")})
        return out

    @staticmethod
    def _read(path, default=None):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return default

    def arrays(self):
        if config.DEV:
            return self._dev_arrays()
        out = []
        try:
            names = [n for n in os.listdir("/sys/block") if n.startswith("md")]
        except OSError:
            return out
        for md in sorted(names):
            base = f"/sys/block/{md}/md"
            if not os.path.isdir(base):
                continue
            level = self._read(f"{base}/level", "")
            if not level:
                continue
            members = []
            for e in sorted(os.listdir(base)):
                if e.startswith("dev-"):
                    members.append({"device": "/dev/" + e[4:], "state": self._read(f"{base}/{e}/state", ""),
                                    "slot": self._read(f"{base}/{e}/slot", "")})
            completed = self._read(f"{base}/sync_completed", "none")
            prog = None
            m = re.match(r"(\d+)\s*/\s*(\d+)", completed or "")
            if m and int(m.group(2)):
                prog = round(100 * int(m.group(1)) / int(m.group(2)), 1)
            # Name aus /dev/md/<name>
            name = md
            try:
                for n in os.listdir("/dev/md"):
                    if os.path.realpath(f"/dev/md/{n}").endswith("/" + md):
                        name = n
            except OSError:
                pass
            size = int(self._read(f"/sys/block/{md}/size", "0") or 0) * 512
            out.append({"name": name, "device": f"/dev/{md}", "md": md, "level": level,
                        "state": self._read(f"{base}/array_state", ""),
                        "degraded": int(self._read(f"{base}/degraded", "0") or 0),
                        "raid_disks": int(self._read(f"{base}/raid_disks", "0") or 0), "size": size,
                        "sync_action": self._read(f"{base}/sync_action", "idle"), "sync_progress": prog,
                        "mismatch": int(self._read(f"{base}/mismatch_cnt", "0") or 0), "members": members})
        return out

    def candidates(self):
        """Platten, die für einen neuen Verbund in Frage kommen (nichts eingehängt, kein Pool, kein DrivePool)."""
        used = {m["uuid"] for p in self.settings.data["pools"] for m in p["members"]}
        res = []
        for d in self.disks.disks:
            if d["system"] or d.get("network") or d.get("raid") or d.get("raid_member"):
                continue
            if config.DEV and any(d["path"] in a["members"] for a in self.settings.data.get("dev_raids", [])):
                continue
            parts = d["partitions"]
            reason = None
            if any(p["uuid"] in used for p in parts):
                reason = "gehört zu einem Pool"
            elif any(p["mountpoint"] for p in parts):
                reason = "ist eingehängt – zuerst aushängen"
            elif any(p["poolparts"] for p in parts):
                reason = "enthält einen DrivePool-Ordner"
            res.append({"name": d["name"], "path": d["path"], "model": d["model"], "serial": d["serial"],
                        "size": d["size"], "partitions": [{"label": p["label"], "fstype": p["fstype"], "size": p["size"]}
                                                          for p in parts], "blocked": reason})
        return res

    # ------------------------------------------------------------ Anlegen
    def create(self, name, level, devices, fs="ext4", confirm=""):
        if self.options["read_only"]:
            raise ValueError("Schreibschutz aktiv – zum Anlegen eines RAID 'read_only' in den App-Optionen ausschalten")
        if not (self.support or self.check_support()).get("available"):
            raise ValueError((self.support or {}).get("reason") or "Software-RAID nicht verfügbar")
        if not NAME_RE.match(name or ""):
            raise ValueError("Name: Buchstaben, Ziffern, - und _ (max. 24 Zeichen)")
        if confirm != name:
            raise ValueError("Zur Bestätigung bitte den Namen des Verbunds eintippen")
        if level not in LEVELS:
            raise ValueError("Unbekannter RAID-Typ")
        if fs not in FILESYSTEMS:
            raise ValueError("Dateisystem muss ext4 oder XFS sein")
        devices = list(dict.fromkeys(devices))
        if len(devices) < LEVELS[level]["min"]:
            raise ValueError(f"{LEVELS[level]['label']} braucht mindestens {LEVELS[level]['min']} Platten")
        if level == "raid10" and len(devices) % 2:
            raise ValueError("RAID 10 braucht eine gerade Anzahl Platten")
        allowed = {c["path"]: c for c in self.candidates()}
        for dev in devices:
            c = allowed.get(dev)
            if not c:
                raise ValueError(f"{dev} kann nicht verwendet werden")
            if c["blocked"]:
                raise ValueError(f"{dev} {c['blocked']}")
        if any(a["name"] == name for a in self.arrays()):
            raise ValueError("Es gibt bereits einen Verbund mit diesem Namen")
        if self.job and self.job.get("running"):
            raise ValueError("Es läuft bereits ein RAID-Auftrag")
        self.job = {"running": True, "kind": "anlegen", "name": name, "phase": "Vorbereiten", "log": [],
                    "started": int(time.time())}
        threading.Thread(target=self._create, args=(name, level, devices, fs), daemon=True, name="raid-create").start()

    def _say(self, text):
        self.job["log"].append(text)
        log.info("RAID %s: %s", self.job.get("name"), text)

    def _create(self, name, level, devices, fs):
        j = self.job
        try:
            if config.DEV:
                sizes = [next(d["size"] for d in self.disks.disks if d["path"] == p) for p in devices]
                with self.settings.lock:
                    self.settings.data.setdefault("dev_raids", []).append(
                        {"name": name, "level": level, "members": devices, "size": capacity(level, sizes),
                         "created": time.time(), "fs": fs})
                    self.settings.save()
                for step in ("Platten leeren", "Verbund anlegen", f"Dateisystem {fs} anlegen"):
                    j["phase"] = step
                    self._say(step + " …")
                    time.sleep(0.5)
                j["result"] = "ok"
                return
            j["phase"] = "Platten leeren"
            for dev in devices:
                run(["mdadm", "--zero-superblock", "--force", dev], timeout=60)
                code, _, err = run(["wipefs", "-a", dev], timeout=120)
                if code != 0:
                    raise RuntimeError(f"wipefs {dev}: {err.strip()}")
                self._say(f"{dev} geleert")
            j["phase"] = "Verbund anlegen"
            md = f"/dev/md/{name}"
            cmd = ["mdadm", "--create", md, "--run", "--metadata=1.2", f"--name={name}",
                   f"--level={level}", f"--raid-devices={len(devices)}"]
            if level in ("raid1", "raid5", "raid6", "raid10"):
                cmd.append("--bitmap=internal")      # schnellerer Wiederaufbau nach unsauberem Abschalten
            code, out, err = run(cmd + devices, timeout=300)
            if code != 0:
                raise RuntimeError((err or out).strip() or "mdadm --create fehlgeschlagen")
            self._say(f"Verbund {md} angelegt ({level}, {len(devices)} Platten)")
            self.save_conf()
            j["phase"] = f"Dateisystem {fs} anlegen"
            label = name[:16] if fs == "ext4" else name[:12]
            mk = ["mkfs.ext4", "-F", "-L", label, "-m", "0", "-E", "lazy_itable_init=1,lazy_journal_init=1", md] \
                if fs == "ext4" else ["mkfs.xfs", "-f", "-L", label, md]
            code, out, err = run(mk, timeout=3600)
            if code != 0:
                raise RuntimeError((err or out).strip() or "Dateisystem anlegen fehlgeschlagen")
            self._say(f"Dateisystem {fs} angelegt – der Verbund ist sofort nutzbar, die Erstsynchronisation läuft im Hintergrund")
            j["result"] = "ok"
        except Exception as err:  # noqa: BLE001
            j["result"] = "fehler"
            j["error"] = str(err)
            self._say(f"FEHLER: {err}")
        finally:
            j["running"] = False
            j["finished"] = int(time.time())
            self.disks.refresh()

    # ------------------------------------------------------------ Verwaltung
    def _array(self, name):
        a = next((x for x in self.arrays() if x["name"] == name), None)
        if not a:
            raise ValueError("Verbund nicht gefunden")
        return a

    def check(self, name):
        """Konsistenzprüfung (liest alle Platten, vergleicht Spiegel/Parität)."""
        a = self._array(name)
        if config.DEV:
            return
        with open(f"/sys/block/{a['md']}/md/sync_action", "w", encoding="utf-8") as fh:
            fh.write("check")

    def replace(self, name, failed_device, new_device):
        if self.options["read_only"]:
            raise ValueError("Schreibschutz aktiv")
        a = self._array(name)
        allowed = {c["path"]: c for c in self.candidates()}
        if new_device not in allowed or allowed[new_device]["blocked"]:
            raise ValueError("Die neue Platte kann nicht verwendet werden")
        if config.DEV:
            return
        if failed_device:
            run(["mdadm", "--manage", a["device"], "--fail", failed_device], timeout=60)
            run(["mdadm", "--manage", a["device"], "--remove", failed_device], timeout=60)
        run(["wipefs", "-a", new_device], timeout=120)
        code, out, err = run(["mdadm", "--manage", a["device"], "--add", new_device], timeout=120)
        if code != 0:
            raise RuntimeError((err or out).strip() or "Hinzufügen fehlgeschlagen")
        self.save_conf()

    def delete(self, name, confirm):
        if confirm != name:
            raise ValueError("Zur Bestätigung bitte den Namen des Verbunds eintippen")
        used = [p["name"] for p in self.settings.data["pools"] for m in p["members"]
                if (self.disks.partition(m["uuid"])[0] or {}).get("path") in (f"/dev/md/{name}",)]
        if used:
            raise ValueError(f"Der Verbund gehört zu Pool {used[0]} – zuerst aus dem Pool nehmen")
        if config.DEV:
            with self.settings.lock:
                self.settings.data["dev_raids"] = [a for a in self.settings.data.get("dev_raids", []) if a["name"] != name]
                self.settings.save()
            return
        a = self._array(name)
        mp = None
        for d in self.disks.disks:
            if d.get("raid") and d["path"] in (a["device"], f"/dev/md/{name}"):
                for p in d["partitions"]:
                    if p["mountpoint"]:
                        mp = p["uuid"]
        if mp:
            self.disks.unmount(mp)
        members = [m["device"] for m in a["members"]]
        code, _, err = run(["mdadm", "--stop", a["device"]], timeout=60)
        if code != 0:
            raise RuntimeError(err.strip() or "Verbund lässt sich nicht stoppen (wird noch benutzt?)")
        for dev in members:
            run(["mdadm", "--zero-superblock", dev], timeout=60)
        self.save_conf()
        self.disks.refresh()

    def status(self):
        return {"support": self.support or self.check_support(), "arrays": self.arrays(),
                "candidates": self.candidates(), "levels": {k: v["label"] for k, v in LEVELS.items()},
                "min": {k: v["min"] for k, v in LEVELS.items()}, "job": self.job,
                "read_only": self.options["read_only"]}
