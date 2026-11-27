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
        self.dirty = {}          # uuid -> Meldung, wenn NTFS als „unsauber“ erkannt wurde
        self.forced_ro = set()   # uuids, die wegen Unsauberkeit nur lesend eingebunden sind
        self.dirty_policy = "readonly"   # readonly | repair | mount (wird von außen gesetzt)
        self.busy = set()        # uuids, die gerade geprüft werden (nicht automatisch einhängen)
        self.allow_hiber = set() # uuids, bei denen der Windows-Ruhezustand verworfen werden darf

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
        md_nodes = {}

        def is_md(node):
            t = (node.get("type") or "")
            return t.startswith("raid") or t in ("linear", "md")

        def collect_md(node):
            for ch in node.get("children") or []:
                if is_md(ch):
                    md_nodes.setdefault(ch.get("name"), ch)
                collect_md(ch)

        for dev in data.get("blockdevices", []):
            collect_md(dev)
            if is_md(dev):
                md_nodes.setdefault(dev.get("name"), dev)
        for dev in data.get("blockdevices", []):
            if dev.get("type") != "disk" or dev.get("name", "").startswith(("loop", "zram", "sr", "ram")):
                continue
            parts = []
            children = [c for c in (dev.get("children") or []) if not is_md(c)]
            raid_member = (dev.get("fstype") == "linux_raid_member" or
                           any(c.get("fstype") == "linux_raid_member" for c in children))
            # Platte ohne Partitionstabelle (Dateisystem direkt auf der Platte)
            if not children and dev.get("fstype") and dev.get("fstype") != "linux_raid_member":
                children = [dict(dev, type="part")]
            system = False
            for ch in children:
                if ch.get("fstype") == "linux_raid_member":
                    continue
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
                "raid_member": raid_member,
                "partitions": parts,
            })
        # Software-RAID-Verbünde als eigene „Platten“
        for name, md in md_nodes.items():
            kids = [c for c in (md.get("children") or []) if not is_md(c)]
            if not kids:
                kids = [dict(md, type="part")]
            disks.append({
                "name": name, "path": md.get("path") or f"/dev/{name}",
                "model": f"RAID {str(md.get('type', '')).replace('raid', '')}".strip() if md.get("type") != "linear" else "JBOD",
                "serial": f"md-{(md.get('uuid') or name)[:12]}", "size": int(md.get("size") or 0),
                "transport": "md", "rotational": False, "system": False, "raid": True, "raid_member": False,
                "partitions": [self._partition(c, mounts) for c in kids],
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
            "dirty": self.dirty.get(uuid),
            "forced_ro": uuid in self.forced_ro,
            "checking": uuid in self.busy,
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
        if part["fstype"] == "ntfs" and not read_only:
            read_only = self._dirty_gate(uuid, part)
        ro = "ro" if read_only else "rw"
        attempts = []
        if part["fstype"] == "ntfs":
            if read_only:
                attempts.append(["mount", "-t", "ntfs3", "-o", f"{ro},iocharset=utf8", part["path"], mp])
            # ntfs-3g ist beim Schreiben der ausgereiftere Treiber
            rw_opts = f"{ro},windows_names,noatime" + (",remove_hiberfile" if uuid in self.allow_hiber else "")
            attempts.append(["ntfs-3g", "-o", rw_opts if not read_only else ro, part["path"], mp])
            if not read_only:
                attempts.append(["mount", "-t", "ntfs3", "-o", f"{ro},iocharset=utf8,noatime", part["path"], mp])
        else:
            attempts.append(["mount", "-t", part["fstype"], "-o", f"{ro},noatime", part["path"], mp])
        last = ""
        for cmd in attempts:
            code, out, err = run(cmd, timeout=120)
            if code == 0:
                actual_ro = self._is_ro(mp)
                if not read_only and actual_ro:
                    # ntfs-3g/ntfs3 weichen bei Windows-Schnellstart/Ruhezustand oder unsauberem Dateisystem
                    # stillschweigend auf „nur lesend“ aus – sichtbar machen statt [rw] zu melden
                    why = " ".join((out + " " + err).split())[:300]
                    self.forced_ro.add(uuid)
                    self.dirty[uuid] = ("Nur lesend eingehängt – Windows wurde nicht vollständig heruntergefahren "
                                        "(Schnellstart/Ruhezustand) oder das Dateisystem ist unsauber. "
                                        "„Prüfen und reparieren“ stellt den Schreibzugriff her."
                                        + (f" Meldung: {why}" if why else ""))
                    log.warning("%s nur lesend eingehängt (vom Treiber erzwungen): %s", part["path"], why or "-")
                    ro = "ro"
                log.info("%s (%s) eingehängt unter %s [%s]", part["path"], part["fstype"], mp, ro)
                self.errors.pop(uuid, None)
                return mp
            last = err.strip()
            log.warning("Einhängen mit %s fehlgeschlagen: %s", cmd[0] if cmd[0] != "mount" else cmd[2], last)
        if "operation not permitted" in last.lower() or "can't open blockdev" in last.lower():
            last = ("Kein Zugriff auf die Platte. Bitte in Home Assistant bei der App RAIDPool den "
                    "„Schutzmodus“ ausschalten und die App neu starten.")
        elif "hibernat" in last.lower() or "unclean" in last.lower() or "dirty" in last.lower():
            last += " – Windows wurde nicht vollständig heruntergefahren (Schnellstart). " \
                    "Platte an Windows sauber herunterfahren oder schreibgeschützt einbinden."
        self.errors[uuid] = last
        try:
            os.rmdir(mp)
        except OSError:
            pass
        raise RuntimeError(last or "Einhängen fehlgeschlagen")

    # ------------------------------------------------------------ Unsaubere NTFS-Platten
    @staticmethod
    def ntfsck_available():
        code, _, _ = run(["ntfsck", "--help"], timeout=10)
        return code != 127

    def is_dirty(self, part):
        """True, wenn das NTFS-Dateisystem als unsauber markiert ist (z. B. nach Stromausfall)."""
        if self.ntfsck_available():
            code, _, _ = run(["ntfsck", "-C", part["path"]], timeout=120)
            return code == 1
        # Fallback: ntfsfix -n meldet bei gesetztem Dirty-Flag entsprechende Hinweise
        code, out, err = run(["ntfsfix", "-n", part["path"]], timeout=120)
        txt = (out + err).lower()
        return "dirty" in txt and "not dirty" not in txt

    def _dirty_gate(self, uuid, part):
        """Vor dem Einhängen mit Schreibzugriff prüfen. Gibt True zurück, wenn nur lesend eingehängt werden soll."""
        try:
            dirty = self.is_dirty(part)
        except Exception as err:  # noqa: BLE001
            log.warning("Dirty-Prüfung %s: %s", part["path"], err)
            return False
        if not dirty:
            self.dirty.pop(uuid, None)
            self.forced_ro.discard(uuid)
            return False
        policy = self.dirty_policy
        if policy == "repair" and self.ntfsck_available():
            log.warning("%s ist unsauber – automatische Prüfung/Reparatur mit ntfsck", part["path"])
            code, out, err = run(["ntfsck", "-a", part["path"]], timeout=6 * 3600)
            if code in (0, 1):
                self.dirty.pop(uuid, None)
                self.forced_ro.discard(uuid)
                return False
            self.dirty[uuid] = f"Automatische Reparatur fehlgeschlagen (Code {code}) – nur lesend eingebunden"
        elif policy == "mount":
            self.dirty[uuid] = "Dateisystem unsauber (z. B. nach Stromausfall) – trotzdem mit Schreibzugriff eingebunden"
            return False
        else:
            self.dirty[uuid] = "Dateisystem unsauber (z. B. nach Stromausfall) – nur lesend eingebunden, bitte prüfen"
        self.forced_ro.add(uuid)
        return True

    def is_writable(self, uuid):
        return not self.options["read_only"] and uuid not in self.forced_ro and uuid not in self.busy

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
                if d.get("raid") and p["fstype"] in ("ext4", "xfs") and not p["mountpoint"] and p["uuid"] not in self.busy:
                    try:
                        self.mount(p["uuid"])
                    except Exception as err:  # noqa: BLE001
                        log.warning("RAID %s nicht einhängbar: %s", d["path"], err)
                    continue
                if p["fstype"] == "ntfs" and not p["mountpoint"] and p["size"] > 1 << 30 and p["uuid"] not in self.busy:
                    try:
                        self.mount(p["uuid"])
                    except Exception as err:  # noqa: BLE001
                        log.warning("Automatisches Einhängen von %s fehlgeschlagen: %s", p["path"], err)
        self.refresh()
