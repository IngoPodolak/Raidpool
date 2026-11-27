"""Werte als Sensoren an Home Assistant übergeben (über die Supervisor-API)."""
import json
import logging
import os
import urllib.request

log = logging.getLogger("nas")

API = "http://supervisor/core/api/states/"


def _post(entity_id, state, attrs):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    body = json.dumps({"state": state, "attributes": attrs}).encode()
    req = urllib.request.Request(API + entity_id, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as err:  # noqa: BLE001
        log.debug("Sensor %s nicht gesendet: %s", entity_id, err)


def _gb(b):
    return round((b or 0) / 1e9, 1)


def publish(pool_states, disks, smart):
    for p in pool_states:
        pid = p["id"]
        sp = p["space"]
        name = p["name"]
        pct = round(100 * (sp["total"] - sp["free"]) / sp["total"], 1) if sp["total"] else 0
        _post(f"sensor.raidpool_{pid}_belegt", pct, {
            "friendly_name": f"RAIDPool {name} belegt", "unit_of_measurement": "%", "icon": "mdi:nas",
            "state_class": "measurement",
            "gesamt_gb": _gb(sp["total"]), "frei_gb": _gb(sp["free"]),
            "daten_gb": _gb(sp["pool_logical"]), "duplikate_gb": _gb(sp["duplication_overhead"]),
            "fremddaten_gb": _gb(sp["other"]), "platten": len(p["members"]),
            "eingehaengt": p["mounted"], "beeintraechtigt": p["degraded"]})
        _post(f"sensor.raidpool_{pid}_frei", _gb(sp["free"]), {
            "friendly_name": f"RAIDPool {name} frei", "unit_of_measurement": "GB",
            "device_class": "data_size", "state_class": "measurement"})
        d = p["dup"]
        _post(f"sensor.raidpool_{pid}_dateien", p["files"], {
            "friendly_name": f"RAIDPool {name} Dateien", "icon": "mdi:file-multiple", "state_class": "measurement",
            "einfach": d["c1"], "zweifach": d["c2"], "dreifach_plus": d["c3"],
            "unter_soll": d["under"], "abweichende_kopien": d["inconsistent"]})
        _post(f"sensor.raidpool_{pid}_unter_soll", d["under"], {
            "friendly_name": f"RAIDPool {name} Dateien unter Soll-Duplizierung", "icon": "mdi:content-duplicate",
            "state_class": "measurement"})
        scan = p.get("scan") or {}
        _post(f"sensor.raidpool_{pid}_scan", scan.get("status") or "nie", {
            "friendly_name": f"RAIDPool {name} Scan", "icon": "mdi:magnify-scan",
            "dateien": scan.get("files"), "beendet": scan.get("finished")})
        _post(f"binary_sensor.raidpool_{pid}_problem", "on" if (p["degraded"] or p["error"] or d["under"]) else "off", {
            "friendly_name": f"RAIDPool {name} Problem", "device_class": "problem",
            "fehler": p["error"], "fehlende_platten": [m["uuid"] for m in p["members"] if not m["available"]]})
    for disk in disks:
        if disk["system"]:
            continue
        key = (disk["serial"] or disk["name"]).lower().replace("-", "_").replace(" ", "_")
        s = smart.get(disk["path"]) or {}
        label = f"{disk['model'] or disk['name']} ({disk['name']})"
        if s.get("temperature") is not None:
            _post(f"sensor.raidpool_disk_{key}_temperatur", s["temperature"], {
                "friendly_name": f"RAIDPool {label} Temperatur", "unit_of_measurement": "°C",
                "device_class": "temperature", "state_class": "measurement"})
        _post(f"sensor.raidpool_disk_{key}_zustand", s.get("state") or "unbekannt", {
            "friendly_name": f"RAIDPool {label} Zustand", "icon": "mdi:harddisk",
            "warnungen": s.get("warnings"), "betriebsstunden": s.get("power_on_hours"),
            "standby": s.get("standby"), "seriennummer": disk["serial"], "groesse_gb": _gb(disk["size"])})
