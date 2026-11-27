"""Pfade, Add-on-Optionen und persistente Einstellungen."""
import json
import logging
import os
import threading

DEV = os.environ.get("NAS_DEV") == "1"
ROOT = os.environ.get("NAS_ROOT", "/tmp/nasdev" if DEV else "")

OPTIONS_FILE = ROOT + "/data/options.json"
DATA_DIR = ROOT + "/data"
CONFIG_DIR = ROOT + "/config"          # addon_config (sichtbar im HA-Ordner addon_configs)
SETTINGS_FILE = CONFIG_DIR + "/nas.json"
INDEX_DB = DATA_DIR + "/index.db"
DISK_MNT = ROOT + "/mnt/nas/disks"
POOL_MNT = ROOT + "/mnt/nas/pools"

DEFAULT_OPTIONS = {
    "read_only": True,
    "auto_mount_ntfs": True,
    "smart_interval_minutes": 30,
    "smart_wake_disks": False,
    "publish_sensors": True,
    "sensor_interval_seconds": 60,
    "log_level": "info",
}

log = logging.getLogger("nas")


def load_options():
    opts = dict(DEFAULT_OPTIONS)
    try:
        with open(OPTIONS_FILE, encoding="utf-8") as fh:
            opts.update(json.load(fh))
    except FileNotFoundError:
        pass
    except Exception as err:  # noqa: BLE001
        log.warning("options.json nicht lesbar: %s", err)
    return opts


class Settings:
    """Vom Nutzer in der Weboberfläche gepflegte Einstellungen (Pools, Soll-Duplizierung)."""

    def __init__(self):
        self._lock = threading.RLock()
        self.data = {"pools": []}
        self.load()

    def load(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except FileNotFoundError:
            pass
        self.data.setdefault("pools", [])

    def save(self):
        with self._lock:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)

    @property
    def lock(self):
        return self._lock

    def pool(self, pool_id):
        for p in self.data["pools"]:
            if p["id"] == pool_id:
                return p
        return None
