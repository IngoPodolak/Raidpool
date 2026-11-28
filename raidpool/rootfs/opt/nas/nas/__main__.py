"""Einstiegspunkt: Hintergrund-Jobs starten und Weboberfläche bereitstellen."""
import json
import logging
import mimetypes
import re
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, sensors
from .disks import DiskManager
from .cache import CacheManager
from .dupes import DupeFinder
from .health import Health
from .mqtt import MqttBridge
from .index import Index
from .netshares import NetShares
from .pools import PoolManager
from .power import PowerManager
from .raid import RaidManager
from .replicate import Replicator
from .smart import SmartMonitor
from .smb import SambaManager

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
INGRESS_IP = "172.30.32.2"
VERSION = "0.20.3"

log = logging.getLogger("nas")


class App:
    def __init__(self):
        self.options = config.load_options()
        logging.basicConfig(level=getattr(logging, self.options["log_level"].upper(), logging.INFO),
                            format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
        for d in (config.DISK_MNT, config.POOL_MNT, config.DATA_DIR):
            os.makedirs(d, exist_ok=True)
        self.settings = config.Settings()
        self.disks = DiskManager(self.options)
        self.raid = RaidManager(self.options, self.settings, self.disks)
        self.net = NetShares(self.options, self.settings)
        self.disks.net = self.net
        self.smart = SmartMonitor(self.options)
        self.index = Index()
        self.pools = PoolManager(self.options, self.settings, self.disks, self.index)
        self.cache = CacheManager(self.options, self.settings, self.pools, self.disks)
        self.pools.cache = self.cache
        self.power = PowerManager(self.options, self.settings, self.disks)
        self.dupes = DupeFinder(self.options, self.settings, self.pools, self.index)
        self.smb = SambaManager(self.options, self.settings, self.pools)
        self.rep = Replicator(self.options, self.settings, self.pools, self.index)
        self.health = Health(self.options, self.settings, self.disks, self.pools, self.rep, self.dupes)
        from .audit import AuditListener
        self.audit = AuditListener(self.rep.external_event)
        self.rep.audit = self.audit
        self.cache.on_moved = lambda pid, rel: self.rep.enqueue(pid, rel)
        from .live import LiveMonitor
        self.live = LiveMonitor(self)
        # Während großer Kopiervorgänge über die Freigaben pausieren Scans, Abgleich und Duplikatsuche
        from .index import Index as _Ix
        _Ix.copy_active = staticmethod(self.live.copy_active)
        self.rep.copy_active = self.live.copy_active
        self.rep.mover_active = self.live.mover_active
        self.dupes.copy_active = self.live.copy_active
        self.cache.rep = self.rep
        self.version = VERSION
        self.health.raid = self.raid
        self.mqtt = MqttBridge(self)
        self.mqtt.cleanup_previous()
        self.stop = threading.Event()

    def protection_mode(self):
        """Schutzmodus der App aktiv? Dann verweigert der Supervisor den Zugriff auf die Platten."""
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token or config.DEV:
            return False
        import urllib.request  # noqa: PLC0415
        try:
            req = urllib.request.Request("http://supervisor/addons/self/info",
                                         headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return bool((json.loads(resp.read()).get("data") or {}).get("protected"))
        except Exception:  # noqa: BLE001
            return False

    def start(self):
        self.protected = self.protection_mode()
        if self.protected:
            log.error("Schutzmodus ist aktiv – ohne vollen Zugriff können keine Platten eingebunden werden. "
                      "Bitte in Home Assistant bei der App RAIDPool den Schutzmodus ausschalten und neu starten.")
        self.net.auto_mount()
        try:
            self.raid.check_support()
            self.raid.assemble()            # vorhandene RAID-Verbünde zusammensetzen
        except Exception:  # noqa: BLE001
            log.exception("RAID-Start")
        if self.options["auto_mount_ntfs"]:
            self.disks.auto_mount()
        else:
            self.disks.refresh()
        self.pools.mount_all()
        self.power.apply_all()
        try:
            self.smb.apply_ips()
            self.audit.start()
            self.smb.audit = self.audit
            self.smb.start()
        except Exception as err:  # noqa: BLE001
            self.smb.error = str(err)
            log.exception("Samba-Start fehlgeschlagen")
        threading.Thread(target=self.smb.watchdog_loop, daemon=True, name="smb-watchdog").start()
        from .supmounts import SupervisorMounts
        self.sup_mounts = SupervisorMounts(self.smb)
        self.sup_mounts.start(self.stop)
        for fn, name in ((self.rep.worker_loop, "rep-worker"), (self.rep.watch_loop, "rep-watch"),
                         (self.rep.scheduler_loop, "rep-scheduler"), (self.health.monitor_loop, "health"),
                         (self.cache.loop, "cache-mover")):
            threading.Thread(target=fn, daemon=True, name=name).start()
        threading.Thread(target=self.power.keepalive_loop, daemon=True, name="keepalive").start()
        self.live.start()
        threading.Thread(target=self.dupes.scheduler_loop, daemon=True, name="dupe-scheduler").start()
        threading.Thread(target=self._smart_loop, daemon=True, name="smart").start()
        if self.options["publish_sensors"]:
            self.mqtt.start()
            threading.Thread(target=self._sensor_loop, daemon=True, name="sensors").start()

    def _smart_loop(self):
        while not self.stop.is_set():
            self.smart.refresh_all(self.disks.disks)
            self.power.apply_all()   # z. B. nach Neuanschluss einer Platte erneut setzen
            self.stop.wait(self.options["smart_interval_minutes"] * 60)

    def _sensor_loop(self):
        while not self.stop.is_set():
            try:
                self.disks.refresh()
                if not self.mqtt.connected:      # mit MQTT übernimmt die Integration die Sensoren
                    sensors.publish(self.pool_states(), self.disks.disks, self.smart)
            except Exception:  # noqa: BLE001
                log.exception("Sensoren konnten nicht aktualisiert werden")
            self.stop.wait(self.options["sensor_interval_seconds"])

    # ------------------------------------------------------------ Daten für API
    def pool_states(self):
        states = []
        for p in self.settings.data["pools"]:
            st = self.pools.status(p)
            st["cache"] = self.cache.status(p)
            for m in st["members"]:
                if m["disk"]:
                    s = self.smart.get(m["disk"])
                    m["smart_state"] = s.get("state") if s else None
            states.append(st)
        return states

    def disk_states(self):
        member_of = {}
        for p in self.settings.data["pools"]:
            for m in p["members"]:
                member_of[m["uuid"]] = p["name"]
        out = []
        for d in self.disks.disks:
            dd = dict(d, smart=self.smart.get(d["path"]), power=self.power.info(d))
            dd["partitions"] = [dict(pt, pool=member_of.get(pt["uuid"])) for pt in d["partitions"]]
            out.append(dd)
        return out

    def drivepool_candidates(self):
        """Alle gefundenen PoolPart-Ordner, die noch keinem Pool zugeordnet sind."""
        used = {(m["uuid"], m["poolpart"]) for p in self.settings.data["pools"] for m in p["members"]}
        res = []
        for d in self.disks.disks:
            for pt in d["partitions"]:
                for pp in pt["poolparts"]:
                    if (pt["uuid"], pp) not in used:
                        res.append({"uuid": pt["uuid"], "poolpart": pp, "disk": d["path"],
                                    "model": d["model"], "label": pt["label"], "usage": pt["usage"]})
        return res

    # ------------------------------------------------------------ API
    def api_get(self, path, q):
        g = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731
        if path == "status":
            return {"version": VERSION, "options": self.options, "dev": config.DEV, "mqtt": self.mqtt.status(),
                    "protected": getattr(self, "protected", False),
                    "pools": self.pool_states(), "candidates": self.drivepool_candidates()}
        if path == "disks":
            self.disks.refresh()
            return self.disk_states()
        if path == "pools":
            return self.pool_states()
        if path == "live":
            return self.live.snapshot()
        if path == "folders":
            pid, fpath = g("pool"), g("path", "")
            pool = self.settings.pool(pid)
            if not pool:
                raise ValueError("Pool nicht gefunden")
            from .index import effective_target, parent_of
            cur = self.index.folder(pid, fpath)
            inherited = effective_target(parent_of(fpath) if fpath else None, pool["targets"],
                                         pool.get("default_target", 1)) if fpath else pool.get("default_target", 1)
            if cur:
                cur["effective_target"] = effective_target(fpath, pool["targets"], pool.get("default_target", 1))
                cur["inherited_target"] = inherited
            children = self.index.children(pid, fpath)
            for c in children:
                c["effective_target"] = effective_target(c["path"], pool["targets"], pool.get("default_target", 1))
            return {"folder": cur, "children": children, "members": len(pool["members"])}
        if path == "files":
            pid, fpath, mode = g("pool"), g("path", ""), g("mode", "direct")
            pool = self.settings.pool(pid)
            if not pool:
                raise ValueError("Pool nicht gefunden")
            limit = min(int(g("limit", 500)), 5000)
            if mode == "under":
                return self.index.under_files(pool, fpath, limit)
            return self.index.files(pid, fpath, mode, limit)
        if path == "raid/status":
            self.disks.refresh()
            return self.raid.status()
        if path == "cache/targets":
            self.disks.refresh()
            return self.cache.targets()
        if path == "health/status":
            return self.health.status()
        if path == "health/ups_candidates":
            return self.health.ups_candidates()
        if path == "health/smart_test":
            return self.health.smart_test_status(g("device"))
        if path == "rep/status":
            return self.rep.status()
        if path == "rep/copies":
            pool = self.settings.pool(g("pool"))
            if not pool:
                raise ValueError("Pool nicht gefunden")
            return self.rep.copies_of(pool, g("path"))
        if path == "smb/status":
            return self.smb.status()
        if path == "smb/ha_users":
            return self.smb.ha_candidates()
        if path == "smb/conf":
            return {"text": self.smb.render_conf()}
        if path == "dupes/status":
            return self.dupes.status()
        if path == "dupes/groups":
            folder = {"pool": g("fpool"), "path": g("fpath", ""), "sub": g("fsub", "1") == "1"} if g("fpool") else None
            return self.dupes.groups(int(g("offset", 0)), min(int(g("limit", 50)), 500), g("search", ""),
                                     g("ignored") == "1", g("sort", "wasted"), folder)
        if path == "dupes/folders":
            return self.dupes.folders()
        if path == "dupes/ignored":
            return self.dupes.ignored_list()
        if path == "browse":
            return self.dupes.browse(g("pool"), g("path", ""))
        if path == "dupes/trash":
            return self.dupes.trash_list()
        if path == "smart":
            return self.smart.get(g("device"))
        if path == "log":
            return {"lines": []}
        raise KeyError(path)

    def api_post(self, path, body):
        if path == "disks/mount":
            self.disks.refresh()
            self.disks.mount(body["uuid"])
            self.disks.refresh()
            return {"ok": True}
        if path == "disks/unmount":
            for p in self.settings.data["pools"]:
                if any(m["uuid"] == body["uuid"] for m in p["members"]) and self.pools.is_mounted(p):
                    raise ValueError(f"Platte gehört zu Pool '{p['name']}' – zuerst Pool aushängen")
            self.disks.unmount(body["uuid"])
            self.disks.refresh()
            return {"ok": True}
        if path.startswith("raid/"):
            self.disks.refresh()
            a = path[5:]
            if a == "create":
                self.raid.create(body.get("name", "").strip(), body.get("level"), body.get("devices") or [],
                                 body.get("fs", "ext4"), body.get("confirm", ""))
            elif a == "check":
                self.raid.check(body["name"])
            elif a == "replace":
                self.raid.replace(body["name"], body.get("failed"), body["device"])
            elif a == "delete":
                self.raid.delete(body["name"], body.get("confirm", ""))
            else:
                raise KeyError(path)
            return {"ok": True}
        if path in ("pools/cache", "pools/cache_flush"):
            pool = self.settings.pool(body.get("pool"))
            if not pool:
                raise ValueError("Pool nicht gefunden")
            if path == "pools/cache_flush":
                self.cache.flush(pool)
                return {"ok": True}
            self.disks.refresh()
            return self.cache.set_cfg(pool["id"], body)
        if path == "health/config":
            return self.health.set_cfg(body)
        if path == "health/check":
            self.health.start_check(body["uuid"], bool(body.get("repair")))
            return {"ok": True}
        if path == "health/smart_test":
            self.health.start_smart_test(body["device"], body.get("kind", "long"))
            return {"ok": True}
        if path in ("pools/add_member", "pools/replace_member"):
            self.disks.refresh()
            if path == "pools/add_member":
                self.pools.add_member(body["pool"], body["uuid"], body.get("mode", "pp"))
            else:
                self.pools.replace_member(body["pool"], body["old"], body["uuid"], body.get("mode", "pp"))
                self.health.failed.pop((body["pool"], body["old"]), None)
            started = False
            if self.rep.writable() and self.rep.cfg()["enabled"]:
                try:
                    self.rep.start_job("abgleich", pool_id=body["pool"])
                    started = True
                except ValueError:
                    pass
            return {"ok": True, "reconcile": started}
        if path == "ha/dashboard":
            from .dashboard import publish
            return publish(self)
        if path == "rep/config":
            return self.rep.set_cfg(body)
        if path == "rep/start":
            kw = {}
            if body.get("pool"):
                kw["pool_id"] = body["pool"]
            if body.get("kind") == "entfernen":
                kw["uuid"] = body["uuid"]
            self.rep.start_job(body["kind"], **kw)
            return {"ok": True}
        if path == "rep/cancel":
            self.rep.cancel.set()
            return {"ok": True}
        if path == "rep/resolve":
            pool = self.settings.pool(body.get("pool"))
            if not pool:
                raise ValueError("Pool nicht gefunden")
            return {"result": self.rep.resolve(pool, body["path"], body.get("uuid"))}
        if path.startswith("smb/"):
            action = path[4:]
            if action == "config":
                return self.smb.set_cfg(body)
            if action == "share/save":
                return self.smb.save_share(body)
            if action == "share/remove":
                self.smb.remove_share(body["id"])
            elif action == "user/add":
                self.smb.add_user(body.get("name"), body.get("password"), body.get("ha", False), body.get("display", ""),
                                  body.get("logins") or [])
            elif action == "user/password":
                self.smb.set_password(body["name"], body.get("password"))
            elif action == "user/enabled":
                self.smb.set_user_enabled(body["name"], body.get("enabled", True))
            elif action == "direct":
                return self.smb.set_direct(body.get("enabled"), body.get("uuid"), body.get("on"),
                                           body.get("access"), body.get("browseable"))
            elif action == "user/logins":
                self.smb.set_logins(body["name"], body.get("logins") or [])
            elif action == "check_name":
                return {"name": body["name"], "taken_by": self.smb.check_name(body["name"])}
            elif action == "remote_shares":
                return self.smb.remote_shares(body.get("host", ""), body.get("user", ""), body.get("password", ""))
            elif action == "import":
                return self.smb.import_shares(body.get("items") or [], body.get("access") or {})
            elif action == "user/remove":
                self.smb.remove_user(body["name"])
            elif action == "start":
                self.smb.restarts = 0
                self.smb.start()
                if self.smb.error:
                    raise RuntimeError(self.smb.error)
            elif action == "stop":
                self.smb.stop()
            elif action == "restart":
                self.smb.restarts = 0
                self.smb.apply(restart=True)
                if self.smb.error:
                    raise RuntimeError(self.smb.error)
            else:
                raise KeyError(path)
            return {"ok": True}
        if path == "net/save":
            share = self.net.save(body)
            if body.get("mount", True):
                try:
                    self.net.mount(share["id"])
                except Exception as err:  # noqa: BLE001
                    self.disks.refresh()
                    raise RuntimeError(f"Gespeichert, aber Einhängen fehlgeschlagen: {err}") from err
            self.disks.refresh()
            return share
        if path == "net/remove":
            self.net.remove(body["id"])
            self.disks.refresh()
            return {"ok": True}
        if path == "dupes/start":
            self.dupes.start("manuell", folders=body.get("folders"), within=body.get("within", False))
            return {"ok": True}
        if path == "dupes/stop":
            self.dupes.stop_scan()
            return {"ok": True}
        if path == "dupes/config":
            return self.dupes.set_cfg(body)
        if path == "dupes/ignore":
            self.dupes.ignore(body["hashes"], body["kind"], body.get("note", ""))
            return {"ok": True}
        if path == "dupes/delete":
            return self.dupes.delete_async(body["items"])
        if path == "dupes/select":
            if body.get("kind") == "pattern":
                return self.dupes.select_pattern()
            return self.dupes.select_folder(body["pool"], body.get("path", ""), body.get("mode", "mark"),
                                            body.get("sub", True))
        if path == "dupes/trash_action":
            return self.dupes.trash_action(body["stamp"], body["action"], body.get("paths"))
        if path == "dupes/purge":
            if self.options["read_only"]:
                raise ValueError("Schreibschutz aktiv")
            self.dupes.purge_trash(int(body.get("days", 0)))
            return {"ok": True}
        if path == "disks/power":
            self.power.set(body["key"], body["mode"], body.get("minutes"))
            return {"ok": True}
        if path == "smart/refresh":
            self.smart.refresh_all(self.disks.disks, wake=bool(body.get("wake")))
            return {"ok": True}
        if path == "pools/create":
            self.disks.refresh()
            return self.pools.create(body["name"].strip(), body["members"], body.get("default_target", 1),
                                     adopt=body.get("adopt", False))
        pool = self.settings.pool(body.get("pool"))
        if not pool:
            raise ValueError("Pool nicht gefunden")
        if path == "pools/delete":
            self.pools.delete(pool["id"])
        elif path == "pools/mount":
            self.pools.mount(pool)
        elif path == "pools/unmount":
            self.pools.unmount(pool)
        elif path == "pools/scan":
            self.pools.start_scan(pool)
        elif path == "pools/target":
            self.pools.set_target(pool["id"], body.get("path", ""), body.get("value"))
        elif path == "pools/default_target":
            with self.settings.lock:
                pool["default_target"] = max(1, min(int(body["value"]), len(pool["members"])))
                self.settings.save()
            self.index.aggregate(pool)
        elif path == "pools/adopt_targets":
            self.pools.adopt_targets(pool["id"])
        elif path == "pools/rename":
            with self.settings.lock:
                pool["name"] = body["name"].strip() or pool["name"]
                self.settings.save()
        else:
            raise KeyError(path)
        return {"ok": True}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RAIDPool/" + VERSION

        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

        def _allowed(self):
            if config.DEV or self.client_address[0] in (INGRESS_IP, "127.0.0.1"):
                return True
            self.send_error(403)
            return False

        def _json(self, code, data):
            body = json.dumps(data, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _api(self, fn, *args):
            try:
                self._json(200, fn(*args))
            except KeyError as e:
                self._json(404, {"error": f"Unbekannt: {e}"})
            except (ValueError, RuntimeError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("API-Fehler")
                self._json(500, {"error": str(e)})

        def do_GET(self):  # noqa: N802
            if not self._allowed():
                return
            u = urlparse(self.path)
            p = u.path
            if p.startswith("/api/"):
                return self._api(app.api_get, p[5:], parse_qs(u.query))
            name = "index.html" if p in ("/", "") else p.lstrip("/")
            full = os.path.realpath(os.path.join(WEB_DIR, name))
            if not full.startswith(os.path.realpath(WEB_DIR)) or not os.path.isfile(full):
                self.send_error(404)
                return
            with open(full, "rb") as fh:
                body = fh.read()
            if name == "index.html":
                # Versionsnummer an Skripte/Styles hängen: nach einem Update lädt der Browser sicher die neuen Dateien
                body = re.sub(rb'(src|href)="([\w./-]+\.(?:js|css))"',
                              lambda m: m.group(1) + b'="' + m.group(2) + b"?v=" + VERSION.encode() + b'"', body)
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            # index.html nie zwischenspeichern, Skripte nur mit passender Versionsnummer
            self.send_header("Cache-Control", "no-cache" if name == "index.html" else "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            if not self._allowed():
                return
            p = urlparse(self.path).path
            if not p.startswith("/api/"):
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "Ungültiges JSON"})
            return self._api(app.api_post, p[5:], body)

    return Handler


def main():
    app = App()
    app.start()
    port = int(os.environ.get("NAS_PORT", "8099"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    server.daemon_threads = True

    def shutdown(*_):
        app.stop.set()
        app.power.stop.set()
        app.dupes.stop.set()
        app.dupes.cancel.set()
        app.smb.stop_evt.set()
        app.rep.stop.set()
        app.health.stop.set()
        app.mqtt.stop.set()
        app.cache.stop.set()
        app.smb.shutdown()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info("Weboberfläche auf Port %s bereit", port)
    server.serve_forever()
    time.sleep(0.2)


if __name__ == "__main__":
    main()
