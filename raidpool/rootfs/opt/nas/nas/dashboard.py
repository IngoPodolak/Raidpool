"""Erzeugt das Home-Assistant-Dashboard „RAIDPool“ mit Tortendiagrammen (apexcharts-card) und Bubble Cards.

Die Entitäts-IDs werden über die Unique-IDs aus der Entitätsregistrierung aufgelöst – so funktioniert das
Dashboard auch, wenn Entitäten in HA umbenannt wurden.
"""
import logging

from . import config
from .mqtt import _slug
from .smb import ha_ws

log = logging.getLogger("nas")

URL_PATH = "raidpool-nas"
COLORS = {"daten": "#03a9f4", "duplikate": "#7c5cff", "sonstiges": "#9aa3ad", "frei": "#dfe3e8", "belegt": "#03a9f4"}
TOTAL_FMT = ("EVAL:function(w){const t=w.globals.seriesTotals.reduce((a,b)=>a+b,0);"
             "return t>=1000?(t/1000).toFixed(1)+' TB':t.toFixed(0)+' GB'}")


def _donut(title, series):
    return {
        "type": "custom:apexcharts-card",
        "chart_type": "donut",
        "header": {"show": True, "title": title, "show_states": False},
        "series": [{"entity": e, "name": n, "color": c, "unit": " GB"} for e, n, c in series],
        "apex_config": {
            "legend": {"position": "bottom"},
            "dataLabels": {"enabled": True},
            "plotOptions": {"pie": {"donut": {"size": "62%", "labels": {
                "show": True, "total": {"show": True, "label": "Gesamt", "formatter": TOTAL_FMT}}}}},
        },
        "grid_options": {"columns": 6},
    }


def _sub(entity, icon, name=None):
    b = {"entity": entity, "icon": icon, "show_state": True, "show_background": False}
    if name:
        b["name"] = name
    return b


def _slider(entity, name, icon, subs):
    return {"type": "custom:bubble-card", "card_type": "button", "button_type": "slider", "entity": entity,
            "name": name, "icon": icon, "show_state": True, "read_only_slider": True,
            "min_value": 0, "max_value": 100, "sub_button": [s for s in subs if s["entity"]],
            "grid_options": {"columns": 12}}


def _switch(entity, name, icon):
    return {"type": "custom:bubble-card", "card_type": "button", "button_type": "switch", "entity": entity,
            "name": name, "icon": icon, "show_state": True, "grid_options": {"columns": 6}}


def _press(entity, name, icon):
    return {"type": "custom:bubble-card", "card_type": "button", "button_type": "name", "name": name, "icon": icon,
            "button_action": {"tap_action": {"action": "perform-action", "perform_action": "button.press",
                                             "target": {"entity_id": entity}}},
            "tap_action": {"action": "perform-action", "perform_action": "button.press",
                           "target": {"entity_id": entity}},
            "grid_options": {"columns": 6}}


def build(app, ids):
    """ids: unique_id -> entity_id. Fehlende Entitäten werden ausgelassen."""
    E = lambda uid, comp="sensor": ids.get(uid) or (f"{comp}.{uid}" if not ids else None)  # noqa: E731
    sections = []

    # Übersicht / Steuerung
    cards = [{"type": "heading", "heading": "RAIDPool", "icon": "mdi:nas"}]
    for uid, name, icon in (("raidpool_smb", "SMB-Freigaben", "mdi:folder-network"),
                            ("raidpool_duplizierung", "Duplizierung", "mdi:content-duplicate"),
                            ("raidpool_direktfreigaben", "Direktfreigaben", "mdi:harddisk")):
        if E(uid, "switch"):
            cards.append(_switch(E(uid, "switch"), name, icon))
    if E("raidpool_problem", "binary_sensor"):
        cards.append({"type": "custom:bubble-card", "card_type": "button", "button_type": "state",
                      "entity": E("raidpool_problem", "binary_sensor"), "name": "Problem", "icon": "mdi:alert-circle-outline",
                      "show_state": True, "grid_options": {"columns": 6}})
    for uid, name, icon in (("raidpool_abgleich", "Abgleich", "mdi:sync"),
                            ("raidpool_ausbalancieren", "Ausbalancieren", "mdi:scale-balance"),
                            ("raidpool_duplikatsuche", "Duplikatsuche", "mdi:file-search"),
                            ("raidpool_smart", "SMART aktualisieren", "mdi:harddisk-plus")):
        if E(uid, "button"):
            cards.append(_press(E(uid, "button"), name, icon))
    sections.append({"type": "grid", "cards": cards})

    # Pools
    for p in app.pool_states():
        o = f"raidpool_{_slug(p['id'])}"
        cards = [{"type": "heading", "heading": f"Pool {p['name']}", "icon": "mdi:database"}]
        series = [(E(f"{o}_daten"), "Daten", COLORS["daten"]), (E(f"{o}_duplikate"), "Duplikate", COLORS["duplikate"]),
                  (E(f"{o}_sonstiges"), "Sonstiges", COLORS["sonstiges"]), (E(f"{o}_frei"), "Frei", COLORS["frei"])]
        series = [x for x in series if x[0]]
        if series:
            cards.append(dict(_donut(p["name"], series), grid_options={"columns": 12}))
        if E(f"{o}_belegt"):
            cards.append(_slider(E(f"{o}_belegt"), f"Pool {p['name']}", "mdi:database",
                                 [_sub(E(f"{o}_frei"), "mdi:database-check", "frei"),
                                  _sub(E(f"{o}_gesamt"), "mdi:database", "gesamt"),
                                  _sub(E(f"{o}_dateien"), "mdi:file-multiple", "Dateien")]))
        for uid, name, icon in ((f"{o}_abgleich", "Abgleich", "mdi:sync"), (f"{o}_scan", "Neu scannen", "mdi:magnify-scan")):
            if E(uid, "button"):
                cards.append(_press(E(uid, "button"), name, icon))
        sections.append({"type": "grid", "cards": cards})

    # Platten
    disk_cards = [{"type": "heading", "heading": "Platten", "icon": "mdi:harddisk"}]
    for d in app.disks.disks:
        if d["system"] or d.get("network"):
            continue
        o = f"raidpool_platte_{_slug(d['serial'] or d['name'])}"
        label = next((pt["label"] for pt in d["partitions"] if pt.get("label")), d["model"] or d["name"])
        if E(f"{o}_belegt"):
            disk_cards.append(_slider(E(f"{o}_belegt"), f"{label} · {d['model'] or d['name']}", "mdi:harddisk",
                                      [_sub(E(f"{o}_temperatur"), "mdi:thermometer"),
                                       _sub(E(f"{o}_frei"), "mdi:harddisk-plus", "frei"),
                                       _sub(E(f"{o}_groesse"), "mdi:harddisk", "Größe")]))
        if E(f"{o}_belegt_gb") and E(f"{o}_frei"):
            disk_cards.append(_donut(label, [(E(f"{o}_belegt_gb"), "Belegt", COLORS["belegt"]),
                                             (E(f"{o}_frei"), "Frei", COLORS["frei"])]))
        if E(f"{o}_direktfreigabe", "switch"):
            disk_cards.append(_switch(E(f"{o}_direktfreigabe", "switch"), f"Direktfreigabe {label}",
                                      "mdi:folder-network-outline"))
    if len(disk_cards) > 1:
        sections.append({"type": "grid", "cards": disk_cards})

    return {"title": "RAIDPool", "views": [{"title": "RAIDPool", "path": "uebersicht", "icon": "mdi:nas",
                                            "type": "sections", "max_columns": 3, "sections": sections}]}


def entity_ids():
    if config.DEV:
        return {}
    reg = ha_ws([{"type": "config/entity_registry/list"}])[0] or []
    return {e["unique_id"]: e["entity_id"] for e in reg
            if e.get("platform") == "mqtt" and str(e.get("unique_id", "")).startswith("raidpool_")}


def publish(app):
    """Dashboard in HA anlegen (falls nötig) und Inhalt schreiben. Gibt die URL zurück."""
    if not app.mqtt.connected and not config.DEV:
        raise RuntimeError("Noch keine Verbindung über MQTT – erst wenn die Entitäten in HA existieren, "
                           "kann das Dashboard erzeugt werden")
    ids = entity_ids()
    if not ids and not config.DEV:
        raise RuntimeError("Die RAIDPool-Entitäten sind in Home Assistant noch nicht angelegt – bitte kurz warten")
    cfg = build(app, ids)
    if config.DEV:
        return {"url": f"/{URL_PATH}", "config": cfg}
    dashboards = ha_ws([{"type": "lovelace/dashboards/list"}])[0] or []
    if not any(d.get("url_path") == URL_PATH for d in dashboards):
        ha_ws([{"type": "lovelace/dashboards/create", "url_path": URL_PATH, "title": "RAIDPool", "icon": "mdi:nas",
                "show_in_sidebar": True, "require_admin": False, "mode": "storage"}])
    ha_ws([{"type": "lovelace/config/save", "url_path": URL_PATH, "config": cfg}])
    log.info("Dashboard /%s in Home Assistant geschrieben", URL_PATH)
    return {"url": f"/{URL_PATH}", "cards": sum(len(s["cards"]) for s in cfg["views"][0]["sections"])}
