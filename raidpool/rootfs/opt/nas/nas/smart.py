"""SMART-Werte über smartctl (JSON). Schlafende Platten werden standardmäßig nicht geweckt."""
import json
import logging
import threading
import time

from . import config
from .util import run

log = logging.getLogger("nas")

# ATA-Attribute, die auf einen Defekt hindeuten
CRITICAL_ATTRS = {
    5: "Neu zugewiesene Sektoren",
    187: "Nicht korrigierbare Fehler",
    188: "Befehls-Zeitüberschreitungen",
    197: "Schwebende Sektoren",
    198: "Nicht korrigierbare Offline-Sektoren",
    199: "CRC-Fehler (Kabel)",
}


def parse(data):
    res = {
        "available": True,
        "passed": (data.get("smart_status") or {}).get("passed"),
        "temperature": (data.get("temperature") or {}).get("current"),
        "power_on_hours": (data.get("power_on_time") or {}).get("hours"),
        "power_cycles": data.get("power_cycle_count"),
        "start_stop": None,
        "load_cycles": None,
        "attributes": [],
        "warnings": [],
        "nvme": None,
    }
    table = (data.get("ata_smart_attributes") or {}).get("table") or []
    for a in table:
        raw = (a.get("raw") or {}).get("value")
        entry = {
            "id": a.get("id"),
            "name": a.get("name"),
            "value": a.get("value"),
            "worst": a.get("worst"),
            "thresh": a.get("thresh"),
            "raw": raw,
            "failing": bool((a.get("when_failed") or "").strip()),
        }
        res["attributes"].append(entry)
        if a.get("id") == 4:
            res["start_stop"] = raw
        elif a.get("id") == 193:
            res["load_cycles"] = raw
        if a.get("id") in CRITICAL_ATTRS and raw:
            if a["id"] == 188 and raw > 0xFFFF:  # herstellerspezifisch gepackt
                continue
            res["warnings"].append(f"{CRITICAL_ATTRS[a['id']]}: {raw}")
        if entry["failing"]:
            res["warnings"].append(f"Attribut {a.get('name')} unter Schwellwert")
    nv = data.get("nvme_smart_health_information_log")
    if nv:
        res["nvme"] = {
            "percentage_used": nv.get("percentage_used"),
            "media_errors": nv.get("media_errors"),
            "data_written_tb": round((nv.get("data_units_written") or 0) * 512000 / 1e12, 2),
            "available_spare": nv.get("available_spare"),
        }
        if nv.get("media_errors"):
            res["warnings"].append(f"Medienfehler: {nv['media_errors']}")
        if (nv.get("percentage_used") or 0) >= 90:
            res["warnings"].append(f"Verschleiß {nv['percentage_used']} %")
    if (res["load_cycles"] or 0) > 300000:
        res["warnings"].append(f"Sehr viele Kopf-Parkvorgänge: {res['load_cycles']} (Nennwert meist 300.000–600.000)")
    if res["passed"] is False:
        res["warnings"].insert(0, "SMART-Gesamtstatus: FEHLER")
    res["state"] = "fehler" if res["passed"] is False else ("warnung" if res["warnings"] else "ok")
    return res


class SmartMonitor:
    def __init__(self, options):
        self.options = options
        self.cache = {}      # device path -> result
        self.lock = threading.Lock()

    def read(self, device, wake=None):
        if wake is None:
            wake = self.options["smart_wake_disks"]
        if config.DEV:
            result = {"available": True, "passed": True, "temperature": 34, "power_on_hours": 12345,
                      "power_cycles": 100, "start_stop": 412, "load_cycles": 2380, "attributes": [], "warnings": [], "nvme": None,
                      "state": "ok", "standby": False, "updated": int(time.time())}
            with self.lock:
                self.cache[device] = result
            return result
        result = None
        for extra in ([], ["-d", "sat"]):
            cmd = ["smartctl", "-j", "-a"] + extra
            if not wake:
                cmd += ["-n", "standby"]
            code, out, _ = run(cmd + [device], timeout=60)
            try:
                data = json.loads(out or "{}")
            except json.JSONDecodeError:
                data = {}
            # Bit 1 (Wert 2) = Gerät im Standby (bei -n standby) bzw. Öffnen fehlgeschlagen
            msgs = " ".join(m.get("string", "") for m in (data.get("smartctl") or {}).get("messages", []))
            if "STANDBY" in msgs.upper() or "SLEEP" in msgs.upper():
                prev = self.cache.get(device) or {"available": True, "state": "unbekannt", "warnings": []}
                result = dict(prev, standby=True)
                break
            if data.get("smart_status") is not None or data.get("ata_smart_attributes") \
                    or data.get("nvme_smart_health_information_log"):
                result = parse(data)
                result["standby"] = False
                break
            result = {"available": False, "state": "nicht verfügbar", "warnings": [],
                      "reason": msgs or f"smartctl Rückgabewert {code}"}
        result["updated"] = int(time.time())
        with self.lock:
            self.cache[device] = result
        return result

    def refresh_all(self, disks, wake=None):
        for d in disks:
            try:
                self.read(d["path"], wake)
            except Exception as err:  # noqa: BLE001
                log.warning("SMART für %s fehlgeschlagen: %s", d["path"], err)

    def get(self, device):
        with self.lock:
            return self.cache.get(device)
