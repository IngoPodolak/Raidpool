"""Integration in Home Assistant über MQTT-Discovery.

Geräte: „HA RAIDPool“ (Add-on), je Pool ein Gerät, je Platte ein Gerät.
Entitäten: Sensoren, Problem-Sensoren, Schalter (Samba, Duplizierung, Direktfreigaben pro Platte)
und Tasten (Abgleich, Ausbalancieren, Scan, Duplikatsuche, SMART).
Der Broker (Mosquitto-Add-on) wird automatisch über den Supervisor gefunden.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.request

from . import config

log = logging.getLogger("nas")

PREFIX = "homeassistant"
BASE = "raidpool"
AVAIL = f"{BASE}/status"


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_") or "x"


def supervisor_mqtt():
    """Zugangsdaten des MQTT-Brokers vom Supervisor (services: mqtt:want)."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    req = urllib.request.Request("http://supervisor/services/mqtt", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return (json.loads(resp.read()) or {}).get("data") or None
    except Exception as err:  # noqa: BLE001
        log.info("Kein MQTT-Broker vom Supervisor: %s", err)
        return None


class MqttBridge:
    def __init__(self, app):
        self.app = app
        self.client = None
        self.connected = False
        self.error = None
        self.stop = threading.Event()
        self.published = set()        # aktuell veröffentlichte Discovery-Topics
        self.lock = threading.Lock()
        self.commands = {}            # Befehls-Topic -> Funktion(payload)

    # ------------------------------------------------------------ Verbindung
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="mqtt").start()

    def _run(self):
        try:
            import paho.mqtt.client as mqtt  # noqa: PLC0415
        except ImportError:
            self.error = "paho-mqtt nicht installiert"
            return
        info = None
        while not self.stop.is_set() and not info:
            info = supervisor_mqtt() if not config.DEV else None
            if not info:
                self.error = "Kein MQTT-Broker gefunden (Mosquitto-Add-on installiert?) – Sensoren werden direkt gesetzt"
                if self.stop.wait(300):
                    return
        try:
            cb = getattr(mqtt, "CallbackAPIVersion", None)
            self.client = mqtt.Client(cb.VERSION2, client_id="ha-raidpool") if cb else mqtt.Client(client_id="ha-raidpool")
        except TypeError:
            self.client = mqtt.Client(client_id="ha-raidpool")
        c = self.client
        if info.get("username"):
            c.username_pw_set(info["username"], info.get("password") or "")
        if info.get("ssl"):
            c.tls_set()
        c.will_set(AVAIL, "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(2, 60)
        while not self.stop.is_set():
            try:
                c.connect(info["host"], int(info.get("port", 1883)), keepalive=60)
                break
            except Exception as err:  # noqa: BLE001
                self.error = f"MQTT-Verbindung fehlgeschlagen: {err}"
                self.stop.wait(30)
        c.loop_start()
        while not self.stop.wait(self.app.options["sensor_interval_seconds"]):
            if self.connected:
                try:
                    self.publish_all()
                except Exception:  # noqa: BLE001
                    log.exception("MQTT-Aktualisierung")
        try:
            c.publish(AVAIL, "offline", retain=True)
            c.loop_stop()
            c.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def _on_connect(self, client, userdata, flags, rc, *args):
        ok = (rc == 0) if isinstance(rc, int) else not getattr(rc, "is_failure", False)
        if not ok:
            self.error = f"MQTT-Anmeldung abgelehnt ({rc})"
            return
        self.connected = True
        self.error = None
        log.info("MQTT verbunden – Entitäten werden in Home Assistant angelegt")
        client.subscribe(f"{BASE}/cmd/#")
        client.subscribe(f"{PREFIX}/status")          # HA-Neustart -> Discovery erneut senden
        client.publish(AVAIL, "online", retain=True)
        if not getattr(self, "_rest_removed", False):
            self._rest_removed = True
            self._remove_rest_sensors()
        threading.Thread(target=self.publish_all, kwargs={"discovery": True}, daemon=True).start()

    def _on_disconnect(self, *args):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "replace")
        if msg.topic == f"{PREFIX}/status":
            if payload == "online":
                threading.Thread(target=self.publish_all, kwargs={"discovery": True}, daemon=True).start()
            return
        fn = self.commands.get(msg.topic)
        if not fn:
            return
        try:
            fn(payload)
            log.info("MQTT-Befehl %s: %s", msg.topic, payload)
        except Exception as err:  # noqa: BLE001
            log.warning("MQTT-Befehl %s fehlgeschlagen: %s", msg.topic, err)
        threading.Thread(target=self.publish_all, daemon=True).start()

    def _pub(self, topic, payload, retain=False):
        if self.client is None:
            return
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        self.client.publish(topic, payload, qos=1 if retain else 0, retain=retain)

    def _remove_rest_sensors(self):
        """Früher direkt gesetzte Sensoren (ohne Gerät) entfernen – werden jetzt per MQTT angelegt."""
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return
        try:
            req = urllib.request.Request("http://supervisor/core/api/states", headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                states = json.loads(resp.read())
            for s in states:
                eid = s.get("entity_id", "")
                name = str((s.get("attributes") or {}).get("friendly_name", ""))
                # die alten, direkt gesetzten Sensoren hießen „RAIDPool …“ bzw. „NAS …“
                if eid.split(".", 1)[-1].startswith(("raidpool_", "nas_")) and name.startswith(("RAIDPool ", "NAS ")):
                    r = urllib.request.Request(f"http://supervisor/core/api/states/{eid}", method="DELETE",
                                               headers={"Authorization": f"Bearer {token}"})
                    try:
                        urllib.request.urlopen(r, timeout=10).read()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as err:  # noqa: BLE001
            log.debug("Alte Sensoren nicht entfernt: %s", err)

    # ------------------------------------------------------------ Entitäten beschreiben
    def entities(self):
        """Liefert (component, object_id, config, state) für alle Entitäten und registriert Befehle."""
        app = self.app
        ents = []
        cmds = {}
        ver = getattr(app, "version", "")
        main_dev = {"identifiers": ["raidpool_main"], "name": "HA RAIDPool", "manufacturer": "Podolan",
                    "model": "NAS-Add-on", "sw_version": ver}

        def add(comp, oid, name, dev, state=None, **cfg):
            ents.append((comp, oid, dict(name=name, device=dev, **cfg), state))

        def switch(oid, name, dev, is_on, fn, icon=None):
            topic = f"{BASE}/cmd/{oid}"
            cmds[topic] = lambda p, fn=fn: fn(p.upper() == "ON")
            add("switch", oid, name, dev, "ON" if is_on else "OFF", command_topic=topic,
                payload_on="ON", payload_off="OFF", state_on="ON", state_off="OFF", icon=icon)

        def button(oid, name, dev, fn, icon=None):
            topic = f"{BASE}/cmd/{oid}"
            cmds[topic] = lambda p, fn=fn: fn()
            add("button", oid, name, dev, None, command_topic=topic, payload_press="PRESS", icon=icon)

        # ---- Add-on-Gerät
        smb = app.smb
        rep = app.rep
        dsum = app.dupes.summary()
        switch("raidpool_smb", "SMB-Freigaben", main_dev, smb.cfg()["enabled"],
               lambda on: smb.set_cfg({"enabled": on}), "mdi:folder-network")
        switch("raidpool_duplizierung", "Duplizierung", main_dev, rep.cfg()["enabled"],
               lambda on: rep.set_cfg({"enabled": on}), "mdi:content-duplicate")
        switch("raidpool_direktfreigaben", "Direktfreigaben", main_dev, smb.direct_cfg()["enabled"],
               lambda on: smb.set_direct(enabled=on), "mdi:harddisk")
        button("raidpool_abgleich", "Abgleich starten", main_dev, lambda: rep.start_job("abgleich"), "mdi:sync")
        button("raidpool_ausbalancieren", "Ausbalancieren", main_dev, lambda: rep.start_job("balance"), "mdi:scale-balance")
        button("raidpool_duplikatsuche", "Duplikatsuche starten", main_dev, lambda: app.dupes.start("manuell"), "mdi:file-search")
        button("raidpool_smart", "SMART aktualisieren", main_dev,
               lambda: app.smart.refresh_all(app.disks.disks), "mdi:harddisk-plus")
        add("sensor", "raidpool_duplikate", "Duplikate belegt", main_dev, round(dsum["wasted"] / 1e9, 2),
            unit_of_measurement="GB", device_class="data_size", state_class="measurement", icon="mdi:file-multiple")
        add("sensor", "raidpool_duplikat_gruppen", "Duplikat-Gruppen", main_dev, dsum["groups"],
            state_class="measurement", icon="mdi:file-compare")
        add("sensor", "raidpool_smb_verbindungen", "SMB-Verbindungen", main_dev, len(smb.sessions()),
            state_class="measurement", icon="mdi:lan-connect")
        rs = rep.status()
        add("sensor", "raidpool_warteschlange", "Duplizierung Warteschlange", main_dev, rs["queue"],
            state_class="measurement", icon="mdi:tray-full")
        job = rs.get("job") or {}
        add("sensor", "raidpool_auftrag", "Laufender Auftrag", main_dev,
            job.get("label") if job.get("running") else "keiner", icon="mdi:progress-wrench")
        hs = app.health.status()
        problem = bool(hs["failed"]) or any(p["degraded"] or p["error"] for p in app.pool_states())
        add("binary_sensor", "raidpool_problem", "Problem", main_dev, "ON" if problem else "OFF",
            device_class="problem", payload_on="ON", payload_off="OFF")
        if hs["ups"].get("state") is not None:
            add("binary_sensor", "raidpool_usv_batterie", "USV im Batteriebetrieb", main_dev,
                "ON" if hs["ups"]["on_battery"] else "OFF", payload_on="ON", payload_off="OFF", icon="mdi:battery-alert")

        # ---- Pools
        for p in app.pool_states():
            pid = p["id"]
            dev = {"identifiers": [f"raidpool_pool_{pid}"], "name": f"Pool {p['name']}", "manufacturer": "Podolan",
                   "model": "Pool", "via_device": "raidpool_main"}
            sp = p["space"]
            pct = round(100 * (sp["total"] - sp["free"]) / sp["total"], 1) if sp["total"] else 0
            o = f"raidpool_{_slug(pid)}"
            add("sensor", f"{o}_belegt", "Belegt", dev, pct, unit_of_measurement="%", state_class="measurement",
                icon="mdi:chart-donut")
            add("sensor", f"{o}_frei", "Frei", dev, round(sp["free"] / 1e9, 1), unit_of_measurement="GB",
                device_class="data_size", state_class="measurement")
            add("sensor", f"{o}_gesamt", "Gesamt", dev, round(sp["total"] / 1e9, 1), unit_of_measurement="GB",
                device_class="data_size")
            add("sensor", f"{o}_daten", "Daten (ohne Kopien)", dev, round(sp["pool_logical"] / 1e9, 2),
                unit_of_measurement="GB", device_class="data_size", state_class="measurement")
            add("sensor", f"{o}_dateien", "Dateien", dev, p["files"], state_class="measurement", icon="mdi:file")
            add("sensor", f"{o}_unter_soll", "Dateien unter Soll", dev, p["dup"]["under"], state_class="measurement",
                icon="mdi:content-duplicate")
            add("sensor", f"{o}_abweichend", "Abweichende Kopien", dev, p["dup"]["inconsistent"],
                state_class="measurement", icon="mdi:file-alert")
            add("binary_sensor", f"{o}_problem", "Problem", dev,
                "ON" if (p["degraded"] or p["error"] or p["dup"]["under"]) else "OFF",
                device_class="problem", payload_on="ON", payload_off="OFF")
            add("binary_sensor", f"{o}_eingehaengt", "Eingehängt", dev, "ON" if p["mounted"] else "OFF",
                device_class="connectivity", payload_on="ON", payload_off="OFF")
            button(f"{o}_scan", "Neu scannen", dev, lambda pid=pid: app.pools.start_scan(app.settings.pool(pid)),
                   "mdi:magnify-scan")
            button(f"{o}_abgleich", "Abgleich", dev, lambda pid=pid: rep.start_job("abgleich", pool_id=pid), "mdi:sync")

        # ---- Platten
        direct = {d["uuid"]: d for d in smb.direct_list()}
        for d in app.disks.disks:
            if d["system"] or d.get("network"):
                continue
            key = _slug(d["serial"] or d["name"])
            dev = {"identifiers": [f"raidpool_disk_{key}"], "name": f"Platte {d['model'] or d['name']}",
                   "manufacturer": (d["model"] or "").split(" ")[0] or None, "model": d["model"] or None,
                   "serial_number": d["serial"] or None, "via_device": "raidpool_main"}
            dev = {k: v for k, v in dev.items() if v}
            o = f"raidpool_platte_{key}"
            s = app.smart.get(d["path"]) or {}
            if s.get("temperature") is not None:
                add("sensor", f"{o}_temperatur", "Temperatur", dev, s["temperature"], unit_of_measurement="°C",
                    device_class="temperature", state_class="measurement")
            add("sensor", f"{o}_zustand", "Zustand", dev, s.get("state") or "unbekannt", icon="mdi:harddisk")
            if s.get("power_on_hours") is not None:
                add("sensor", f"{o}_betriebsstunden", "Betriebsstunden", dev, s["power_on_hours"],
                    unit_of_measurement="h", device_class="duration", state_class="total_increasing",
                    entity_category="diagnostic")
            if s.get("load_cycles") is not None:
                add("sensor", f"{o}_parkvorgaenge", "Kopf-Parkvorgänge", dev, s["load_cycles"],
                    state_class="total_increasing", entity_category="diagnostic", icon="mdi:harddisk-plus")
            if s.get("start_stop") is not None:
                add("sensor", f"{o}_anlaeufe", "Anläufe", dev, s["start_stop"], state_class="total_increasing",
                    entity_category="diagnostic", icon="mdi:restart")
            usage = next((pt["usage"] for pt in d["partitions"] if pt.get("usage")), None)
            if usage:
                add("sensor", f"{o}_belegt", "Belegt", dev, round(100 * usage["used"] / usage["total"], 1),
                    unit_of_measurement="%", state_class="measurement", icon="mdi:chart-donut")
            failed = any(u == pt["uuid"] for (_, u) in app.health.failed for pt in d["partitions"])
            add("binary_sensor", f"{o}_problem", "Problem", dev,
                "ON" if (failed or s.get("state") in ("warnung", "fehler") or
                         any(pt.get("dirty") for pt in d["partitions"])) else "OFF",
                device_class="problem", payload_on="ON", payload_off="OFF")
            for pt in d["partitions"]:
                ds = direct.get(pt["uuid"])
                if ds:
                    switch(f"{o}_direktfreigabe", "Direktfreigabe", dev, ds["on"],
                           lambda on, u=pt["uuid"]: smb.set_direct(uuid=u, on=on), "mdi:folder-network-outline")
        return ents, cmds

    # ------------------------------------------------------------ Veröffentlichen
    def publish_all(self, discovery=False):
        if not self.connected:
            return
        with self.lock:
            ents, cmds = self.entities()
            self.commands = cmds
            topics = set()
            for comp, oid, cfg, state in ents:
                topic = f"{PREFIX}/{comp}/{oid}/config"
                topics.add(topic)
                st_topic = f"{BASE}/state/{oid}"
                if discovery or topic not in self.published:
                    full = {k: v for k, v in cfg.items() if v is not None}
                    full.update(unique_id=oid, default_entity_id=f"{comp}.{oid}", availability_topic=AVAIL,
                                has_entity_name=True)
                    if comp != "button":
                        full["state_topic"] = st_topic
                    if "via_device" in full.get("device", {}) and not full["device"]["via_device"]:
                        full["device"].pop("via_device")
                    self._pub(topic, full, retain=True)
                if state is not None:
                    self._pub(st_topic, str(state), retain=True)
            # nicht mehr vorhandene Entitäten (z. B. entfernte Platte) aus HA löschen
            for old in self.published - topics:
                self._pub(old, "", retain=True)
            self.published = topics
            with self.app.settings.lock:
                self.app.settings.data["mqtt_topics"] = sorted(topics)
                self.app.settings.save()

    def cleanup_previous(self):
        """Beim Start: Topics aus dem letzten Lauf kennen, damit Entfernte gelöscht werden können."""
        self.published = set(self.app.settings.data.get("mqtt_topics") or [])

    def status(self):
        return {"connected": self.connected, "error": self.error, "entities": len(self.published)}
