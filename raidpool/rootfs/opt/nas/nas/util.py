import logging
import re
import subprocess
import time

log = logging.getLogger("nas")


def run(cmd, timeout=60, check=False):
    """Befehl ausführen, gibt (returncode, stdout, stderr) zurück."""
    log.debug("run: %s", " ".join(cmd))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} nicht gefunden"
    except subprocess.TimeoutExpired:
        return 124, "", "Zeitüberschreitung"
    if check and res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"{cmd[0]} Fehler {res.returncode}")
    return res.returncode, res.stdout, res.stderr


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "pool"


def now():
    return int(time.time())


def read_mounts():
    """Mountpoint -> (device, fstype) aus /proc/mounts."""
    result = {}
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3:
                    mp = parts[1].replace("\\040", " ")
                    result[mp] = (parts[0], parts[2])
    except FileNotFoundError:
        pass
    return result
