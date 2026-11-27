"""Erkennung von Festplatten/Partitionen und Einhängen einzelner Platten."""
import json
import logging
import os
import threading

from . import config
from .util import read_mounts, run

log = logging.getLogger("nas")

SUPPORTED_FS = {"ntfs", "ext4", "xfs", "btrfs", "exfat"}
POOLPART_PREFIX = "PoolPart."


def _usage(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return {"total": total, "free": free, "used": total - st.f_bfree * st.f_frsize}


def find_poolparts(mountpoint):
    """DrivePool-Ordner (PoolPart.<GUID>) im Wurzelverzeichnis einer Platte."""
    found = []
    try:
        with os.scandir(mountpoint) as it:
            for entry in it:
                if entry.name.startswith(POOLPART_PREFIX) and entry.is_dir(follow_symlinks=False):
                    found.append(entry.name)
    except OSError:
        pass
    return sorted(found)


class DiskManager:
    def __init__(self, options):
        self.options = options
        self.lock = threading.RLock()
        self.disks = []          # zuletzt erkannte Platten
        self.errors = {}         # uuid -> letzte Fehlermeldung beim Einhängen
        self.net = None          # NetShares (Netzwerkfreigaben als „Platten“)

    # ------------------------------------------------------------ Erkennung
    def _lsblk(self):
        if config.DEV:
            with open(config.ROOT + "/fakedisks.json", encoding="utf-8") as fh:
                return json.load(fh)
        cols = "NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,UUID,PARTUUID,MODEL,SERIAL,TRAN,ROTA,MOUNTPOINT,PKNAME"
        code, out, err = run(["lsblk", "-J", "-b", "-o", cols])
        if code != 0:
            log.error("lsblk fehlgeschlagen: %s", err)
            return {"blockdevices": []}
        return json.loads(out)

    def refresh(self):
        data = self._lsblk()
        mounts = read_mounts()
        disks = []
        for dev in data.get("blockdevices", []):
            if dev.get("type") != "disk" or dev.get("name", "").startswith(("loop", "zram", "sr", "ram")):
                continue
            parts = []
            children = dev.get("children") or []
            # Platte ohne Partitionstabelle (Dateisystem direkt auf der Platte)
            if not children and dev.get("fstype"):
                children = [dict(dev, type="part")]
            system = False
            for ch in children:
                label = (ch.get("label") or "")
                if label.startswith("hassos") or ch.get("mountpoint") in ("/", "/data", "/mnt/data"):
                    system = True
                parts.append(self._partition(ch, mounts))
            disks.append({
                "name": dev.get("name"),
                "path": dev.get("path") or "/dev/" + dev.get("name", ""),
                "model": (dev.get("model") or "").strip(),
                "serial": (dev.get("serial") or "").strip(),
                "size": int(dev.get("size") or 0),
                "transport": dev.get("tran") or "",
                "rotational": str(dev.get("rota")) in ("1", "True", "true"),
                "system": system,
                "partitions": parts,
            })
        if self.net:
            disks += self.net.as_disks(_usage, find_poolparts)
        with self.lock:
            self.disks = disks
        return disks

    def _partition(self, ch, mounts):
        uuid = ch.get("uuid") or ch.get("partuuid") or ch.get("name")
        mp = os.path.join(config.DISK_MNT, uuid)
        mounted = mp in mounts or (config.DEV and os.path.isdir(mp))
        part = {
            "name": ch.get("name"),
            "path": ch.get("path") or "/dev/" + ch.get("name", ""),
            "uuid": uuid,
            "fstype": ch.get("fstype") or "",
            "label": ch.get("label") or "",
            "size": int(ch.get("size") or 0),
            "mountpoint": mp if mounted else None,
            "mount_ro": None,
            "poolparts": [],
            "usage": None,
            "error": self.errors.get(uuid),
        }
        if mounted:
            if mp in mounts:
                part["mount_ro"] = self._is_ro(mp)
            part["poolparts"] = find_poolparts(mp)
            part["usage"] = _usage(mp)
        return part

    @staticmethod
    def _is_ro(mp):
        try:
            with open("/proc/mounts", encoding="utf-8") as fh:
                for line in fh:
                    p = line.split()
                    if len(p) >= 4 and p[1] == mp:
                        return "ro" in p[3].split(",")
        except OSError:
            pass
        return None

    def partition(self, uuid):
        with self.lock:
            for d in self.disks:
                for p in d["partitions"]:
                    if p["uuid"] == uuid:
                        return d, p
        return None, None

    # ------------------------------------------------------------ Einhängen
    def mount(self, uuid, read_only=None):
        if read_only is None:
            read_only = self.options["read_only"]
        if uuid.startswith("net-") and self.net:
            return self.net.mount(uuid[4:], read_only)
        disk, part = self.partition(uuid)
        if not part:
            raise ValueError("Partition nicht gefunden")
        if disk["system"]:
            raise ValueError("Systemplatte wird nicht eingebunden")
        if part["fstype"] not in SUPPORTED_FS:
            raise ValueError(f"Dateisystem '{part['fstype'] or 'unbekannt'}' wird nicht unterstützt")
        mp = os.path.join(config.DISK_MNT, uuid)
        if part["mountpoint"]:
            return mp
        if config.DEV:
            os.makedirs(mp, exist_ok=True)
            return mp
        os.makedirs(mp, exist_ok=True)
        ro = "ro" if read_only else "rw"
        attempts = []
        if part["fstype"] == "ntfs":
            if read_only:
                attempts.append(["mount", "-t", "ntfs3", "-o", f"{ro},iocharset=utf8", part["path"], mp])
            # ntfs-3g ist beim Schreiben der ausgereiftere Treiber
            attempts.append(["ntfs-3g", "-o", f"{ro},windows_names,noatime" if not read_only else ro,
                             part["path"], mp])
            if not read_only:
                attempts.append(["mount", "-t", "ntfs3", "-o", f"{ro},iocharset=utf8,noatime", part["path"], mp])
        else:
            attempts.append(["mount", "-t", part["fstype"], "-o", f"{ro},noatime", part["path"], mp])
        last = ""
        for cmd in attempts:
            code, _, err = run(cmd, timeout=120)
            if code == 0:
                log.info("%s (%s) eingehängt unter %s [%s]", part["path"], part["fstype"], mp, ro)
                self.errors.pop(uuid, None)
                return mp
            last = err.strip()
            log.warning("Einhängen mit %s fehlgeschlagen: %s", cmd[0] if cmd[0] != "mount" else cmd[2], last)
        if "hibernat" in last.lower() or "unclean" in last.lower() or "dirty" in last.lower():
            last += " – Windows wurde nicht vollständig heruntergefahren (Schnellstart). " \
                    "Platte an Windows sauber herunterfahren oder schreibgeschützt einbinden."
        self.errors[uuid] = last
        try:
            os.rmdir(mp)
        except OSError:
            pass
        raise RuntimeError(last or "Einhängen fehlgeschlagen")

    def unmount(self, uuid):
        if uuid.startswith("net-") and self.net:
            return self.net.unmount(uuid[4:])
        mp = os.path.join(config.DISK_MNT, uuid)
        if config.DEV:
            return
        code, _, err = run(["umount", mp], timeout=120)
        if code != 0:
            raise RuntimeError(err.strip() or "Aushängen fehlgeschlagen (Platte wird noch benutzt?)")
        try:
            os.rmdir(mp)
        except OSError:
            pass

    def auto_mount(self):
        """Alle NTFS-Datenplatten einhängen, um vorhandene DrivePool-Ordner zu finden."""
        for d in self.refresh():
            if d["system"] or d.get("network"):
                continue
            for p in d["partitions"]:
                if p["fstype"] == "ntfs" and not p["mountpoint"] and p["size"] > 1 << 30:
                    try:
                        self.mount(p["uuid"])
                    except Exception as err:  # noqa: BLE001
                        log.warning("Automatisches Einhängen von %s fehlgeschlagen: %s", p["path"], err)
        self.refresh()
