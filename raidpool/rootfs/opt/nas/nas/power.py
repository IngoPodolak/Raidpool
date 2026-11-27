"""Energieverwaltung der Platten: Dauerbetrieb (kein Abschalten, kein Kopfparken) oder Abschalten nach Zeit."""
import logging
import mmap
import os
import random
import threading

from . import config
from .util import run

log = logging.getLogger("nas")

MODES = ("always", "vendor", "spindown")
KEEPALIVE_SECONDS = 180   # USB-Gehäuse schalten oft selbst nach 5–10 min ab


def disk_key(disk):
    return disk.get("serial") or disk.get("name")


def standby_value(minutes):
    """hdparm -S Wert: 1–240 = x·5 s, 241–251 = (x−240)·30 min."""
    m = max(1, int(minutes))
    if m <= 20:
        return m * 12
    return min(251, 240 + max(1, round(m / 30)))


class PowerManager:
    def __init__(self, options, settings, disks):
        self.options = options
        self.settings = settings
        self.disks = disks
        self.status = {}      # key -> {"applied": ..., "error": ...}
        self.stop = threading.Event()

    # ------------------------------------------------------------ Einstellungen
    def setting(self, disk):
        p = self.settings.data.setdefault("power", {})
        s = (p.get("disks") or {}).get(disk_key(disk))
        if s:
            return dict(s, inherited=False)
        return {"mode": self.options.get("power_default", "always"),
                "minutes": self.options.get("spindown_minutes", 60), "inherited": True}

    def set(self, key, mode, minutes=None):
        if mode not in MODES and mode != "default":
            raise ValueError("Ungültiger Modus")
        with self.settings.lock:
            disks = self.settings.data.setdefault("power", {}).setdefault("disks", {})
            if mode == "default":
                disks.pop(key, None)
            else:
                disks[key] = {"mode": mode, "minutes": int(minutes or 60)}
            self.settings.save()
        for d in self.disks.disks:
            if disk_key(d) == key:
                self.apply(d)

    # ------------------------------------------------------------ Anwenden
    def apply(self, disk):
        if disk["system"]:
            return
        s = self.setting(disk)
        key = disk_key(disk)
        if config.DEV:
            self.status[key] = {"applied": s["mode"], "error": None}
            return
        errors = []
        if s["mode"] == "always":
            cmds = [["hdparm", "-S", "0", disk["path"]],      # kein Standby-Timer
                    ["hdparm", "-B", "254", disk["path"]]]    # APM max. Leistung -> kein Kopfparken
        elif s["mode"] == "spindown":
            cmds = [["hdparm", "-B", "127", disk["path"]],
                    ["hdparm", "-S", str(standby_value(s["minutes"])), disk["path"]]]
        else:
            cmds = []
        for cmd in cmds:
            code, out, err = run(cmd, timeout=30)
            msg = (err or out).strip()
            if code != 0 or "not supported" in msg.lower() or "failed" in msg.lower():
                errors.append(f"{cmd[1]}: {msg.splitlines()[-1] if msg else 'Fehler'}")
        self.status[key] = {"applied": s["mode"], "error": "; ".join(errors) or None}
        if errors:
            log.info("%s: hdparm teilweise nicht unterstützt (%s) – Wachhalten übernimmt", disk["path"], errors)

    def apply_all(self):
        for d in self.disks.disks:
            try:
                self.apply(d)
            except Exception as err:  # noqa: BLE001
                log.warning("Energieeinstellung %s fehlgeschlagen: %s", d["path"], err)

    # ------------------------------------------------------------ Wachhalten
    def _touch(self, disk):
        """Liest 4 KiB direkt von der Platte (am Cache vorbei), damit sie nicht einschläft."""
        size = disk["size"] or 0
        if size < 1 << 20:
            return
        offset = random.randrange(0, size - 4096) // 4096 * 4096
        buf = mmap.mmap(-1, 4096)
        try:
            fd = os.open(disk["path"], os.O_RDONLY | getattr(os, "O_DIRECT", 0))
        except OSError:
            fd = os.open(disk["path"], os.O_RDONLY)
        try:
            os.preadv(fd, [buf], offset)
        finally:
            os.close(fd)
            buf.close()

    def keepalive_loop(self):
        while not self.stop.wait(KEEPALIVE_SECONDS):
            if config.DEV:
                continue
            for d in list(self.disks.disks):
                if d["system"] or self.setting(d)["mode"] != "always":
                    continue
                try:
                    self._touch(d)
                except Exception as err:  # noqa: BLE001
                    log.debug("Wachhalten %s: %s", d["path"], err)

    def info(self, disk):
        s = self.setting(disk)
        st = self.status.get(disk_key(disk)) or {}
        return {**s, "key": disk_key(disk), "error": st.get("error")}
