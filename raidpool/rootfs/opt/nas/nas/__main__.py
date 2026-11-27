"""Einstiegspunkt: Hintergrund-Jobs starten und Weboberfläche bereitstellen."""
import json
import logging
import mimetypes
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, sensors
from .disks import DiskManager
from .dupes import DupeFinder
from .index import Index
from .pools import PoolManager
from .power import PowerManager
from .smart import SmartMonitor

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
INGRESS_IP = "172.30.32.2"
VERSION = "0.3.0"

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
        self.smart = SmartMonitor(self.options)
        self.index = Index()
        self.pools = PoolManager(self.options, self.settings, self.disks, self.index)
        self.power = PowerManager(self.options, self.settings, self.disks)
        self.dupes = DupeFinder(self.options, self.settings, self.pools, self.index)
        self.stop = threading.Event()

    def start(self):
        if self.options["auto_mount_ntfs"]:
            self.disks.auto_mount()
        else:
            self.disks.refresh()
        self.pools.mount_all()
        self.power.apply_all()
        threading.Thread(target=self.power.keepalive_loop, daemon=True, name="keepalive").start()
        threading.Thread(target=self.dupes.scheduler_loop, daemon=True, name="dupe-scheduler").start()
        threading.Thread(target=self._smart_loop, daemon=True, name="smart").start()
        if self.options["publish_sensors"]:
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
                sensors.publish(self.pool_states(), self.disks.disks, self.smart)
            except Exception:  # noqa: BLE001
                log.exception("Sensoren konnten nicht aktualisiert werden")
            self.stop.wait(self.options["sensor_interval_seconds"])

    # ------------------------------------------------------------ Daten für API
    def pool_states(self):
        states = []
        for p in self.settings.data["pools"]:
            st = self.pools.status(p)
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
            return {"version": VERSION, "options": self.options, "dev": config.DEV,
                    "pools": self.pool_states(), "candidates": self.drivepool_candidates()}
        if path == "disks":
            self.disks.refresh()
            return self.disk_states()
        if path == "pools":
            return self.pool_states()
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
        if path == "dupes/status":
            return self.dupes.status()
        if path == "dupes/groups":
            return self.dupes.groups(int(g("offset", 0)), min(int(g("limit", 50)), 500), g("search", ""),
                                     g("ignored") == "1")
        if path == "dupes/trash":
            return self.dupes.trash_info()
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
        if path == "dupes/start":
            self.dupes.start("manuell")
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
            return self.dupes.delete(body["items"])
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
        server_version = "HARAIDPool/" + VERSION

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
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
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
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info("Weboberfläche auf Port %s bereit", port)
    server.serve_forever()
    time.sleep(0.2)


if __name__ == "__main__":
    main()
