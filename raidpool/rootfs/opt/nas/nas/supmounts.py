"""Netzwerkspeicher in Home Assistant, die auf RAIDPool selbst zeigen (z. B. \\\\SERVER\\Backup für
Backups oder einen Download-Ordner), werden beim Start von Home Assistant eingehängt, bevor RAIDPool
läuft – und schlagen dann fehl. RAIDPool lädt sie deshalb neu, sobald Samba bereit ist."""
import json
import logging
import os
import socket
import subprocess
import threading
import urllib.request

from . import config

log = logging.getLogger("nas")


def _sup(method, path):
    token = os.environ.get("SUPERVISOR_TOKEN")
    req = urllib.request.Request(f"http://supervisor{path}", method=method,
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
    return (json.loads(body).get("data") or {}) if body else {}


class SupervisorMounts:
    def __init__(self, smb):
        self.smb = smb
        self.last = []          # [{name, server, share, state, action}]
        self.error = None

    def identities(self):
        names = {"localhost", "127.0.0.1", socket.gethostname().lower(), "homeassistant",
                 "homeassistant.local"}
        try:
            names.add(self.smb.server_name().lower())
            c = self.smb.cfg()
            names |= {a.lower() for a in c.get("aliases") or []}
            names |= {str(ip).split("/")[0] for ip in c.get("extra_ips") or []}
        except Exception:  # noqa: BLE001
            pass
        try:
            out = subprocess.run(["ip", "-o", "-4", "addr"], capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                parts = line.split()
                if "inet" in parts:
                    names.add(parts[parts.index("inet") + 1].split("/")[0])
        except Exception:  # noqa: BLE001
            pass
        return names | {n + ".local" for n in names if "." not in n}

    def check(self):
        """Eigene, nicht eingehängte Netzwerkspeicher neu laden. Gibt Anzahl der Neu-Ladungen zurück."""
        if config.DEV or not os.environ.get("SUPERVISOR_TOKEN"):
            return 0
        try:
            mounts = _sup("GET", "/mounts").get("mounts") or []
        except Exception as err:  # noqa: BLE001
            self.error = f"Netzwerkspeicher von Home Assistant nicht lesbar: {err}"
            return 0
        self.error = None
        ids = self.identities()
        done, res = 0, []
        for m in mounts:
            server = str(m.get("server") or "").lower()
            if server not in ids:
                continue
            entry = {"name": m.get("name"), "server": m.get("server"), "share": m.get("share"),
                     "usage": m.get("usage"), "state": m.get("state"), "action": None}
            if m.get("state") != "active":
                try:
                    _sup("POST", f"/mounts/{m['name']}/reload")
                    entry["action"] = "neu geladen"
                    done += 1
                    log.info("Netzwerkspeicher %s (\\\\%s\\%s) in Home Assistant neu geladen",
                             m.get("name"), m.get("server"), m.get("share"))
                except Exception as err:  # noqa: BLE001
                    entry["action"] = f"Fehler: {err}"
            res.append(entry)
        self.last = res
        return done

    def loop(self, stop):
        stop.wait(20)                       # Samba + Pool kurz anlaufen lassen
        while not stop.is_set():
            self.check()
            stop.wait(120)

    def start(self, stop):
        threading.Thread(target=self.loop, args=(stop,), daemon=True, name="ha-mounts").start()
