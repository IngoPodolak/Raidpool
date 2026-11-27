"""Netzwerkfreigaben (SMB/CIFS und NFS) – werden wie eine Platte behandelt (uuid = net-<id>)."""
import json
import logging
import os
import re
import uuid as uuidlib

from . import config
from .util import read_mounts, run, slug

log = logging.getLogger("nas")

SECRETS_FILE = config.DATA_DIR + "/netshares-secrets.json"   # nicht im sichtbaren Konfig-Ordner
CRED_DIR = config.DATA_DIR + "/net"
PREFIX = "net-"


def normalize(kind, server, share):
    server = server.strip().replace("\\", "/").strip("/")
    share = share.strip().replace("\\", "/").strip("/")
    # Eingabe wie "//server/freigabe" oder "server:/pfad" im Serverfeld zulassen
    if kind == "smb" and "/" in server and not share:
        server, share = server.split("/", 1)
    if kind == "nfs" and ":" in server and not share:
        server, share = server.split(":", 1)
        share = share.strip("/")
    return server, share


class NetShares:
    def __init__(self, options, settings):
        self.options = options
        self.settings = settings
        self.errors = {}

    # ------------------------------------------------------------ Daten
    def list(self):
        return self.settings.data.setdefault("netshares", [])

    def get(self, sid):
        for s in self.list():
            if s["id"] == sid:
                return s
        return None

    def _secrets(self):
        try:
            with open(SECRETS_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_secret(self, sid, password):
        sec = self._secrets()
        if password is None:
            sec.pop(sid, None)
        else:
            sec[sid] = password
        os.makedirs(config.DATA_DIR, exist_ok=True)
        fd = os.open(SECRETS_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(sec, fh)
        os.replace(SECRETS_FILE + ".tmp", SECRETS_FILE)

    @staticmethod
    def uuid(share):
        return PREFIX + share["id"]

    def mountpoint(self, share):
        return os.path.join(config.DISK_MNT, self.uuid(share))

    def is_mounted(self, share):
        mp = self.mountpoint(share)
        if config.DEV:
            return os.path.isdir(mp)
        return mp in read_mounts()

    # ------------------------------------------------------------ Anlegen / Ändern
    def save(self, data):
        kind = data.get("kind", "smb")
        if kind not in ("smb", "nfs"):
            raise ValueError("Typ muss SMB oder NFS sein")
        server, share = normalize(kind, data.get("server", ""), data.get("share", ""))
        if not server or not share:
            raise ValueError("Server und Freigabe angeben")
        if not re.match(r"^[A-Za-z0-9._:\-\[\]]+$", server):
            raise ValueError("Ungültiger Servername")
        with self.settings.lock:
            sid = data.get("id")
            existing = self.get(sid) if sid else None
            if existing and self.is_mounted(existing):
                raise ValueError("Freigabe zuerst aushängen, dann bearbeiten")
            entry = existing or {"id": slug(data.get("name") or server)[:20] + "-" + uuidlib.uuid4().hex[:6]}
            entry.update({
                "name": (data.get("name") or f"{server}/{share}").strip(),
                "kind": kind,
                "server": server,
                "share": share,
                "username": (data.get("username") or "").strip(),
                "domain": (data.get("domain") or "").strip(),
                "version": data.get("version") or "",
                "automount": bool(data.get("automount", True)),
            })
            if not existing:
                self.list().append(entry)
            self.settings.save()
        if data.get("password") is not None and data.get("password") != "":
            self._save_secret(entry["id"], data["password"])
        elif not existing:
            self._save_secret(entry["id"], "")
        return entry

    def remove(self, sid):
        share = self.get(sid)
        if not share:
            raise ValueError("Freigabe nicht gefunden")
        for p in self.settings.data["pools"]:
            if any(m["uuid"] == self.uuid(share) for m in p["members"]):
                raise ValueError(f"Freigabe gehört zu Pool '{p['name']}'")
        if self.is_mounted(share):
            self.unmount(sid)
        with self.settings.lock:
            self.settings.data["netshares"] = [s for s in self.list() if s["id"] != sid]
            src = (self.settings.data.get("dupes") or {}).get("sources") or []
            if self.uuid(share) in src:
                self.settings.data["dupes"]["sources"] = [x for x in src if x != self.uuid(share)]
            self.settings.save()
        self._save_secret(sid, None)
        try:
            os.remove(os.path.join(CRED_DIR, sid))
        except OSError:
            pass

    # ------------------------------------------------------------ Einhängen
    def mount(self, sid, read_only=None):
        share = self.get(sid)
        if not share:
            raise ValueError("Freigabe nicht gefunden")
        if read_only is None:
            read_only = self.options["read_only"]
        mp = self.mountpoint(share)
        if self.is_mounted(share):
            return mp
        os.makedirs(mp, exist_ok=True)
        if config.DEV:
            return mp
        ro = "ro" if read_only else "rw"
        if share["kind"] == "smb":
            os.makedirs(CRED_DIR, exist_ok=True)
            cred = os.path.join(CRED_DIR, share["id"])
            fd = os.open(cred, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                if share["username"]:
                    fh.write(f"username={share['username']}\npassword={self._secrets().get(share['id'], '')}\n")
                    if share["domain"]:
                        fh.write(f"domain={share['domain']}\n")
            opts = [ro, "iocharset=utf8", "uid=0", "gid=0", "file_mode=0664", "dir_mode=0775",
                    "noperm", "nobrl", "actimeo=30"]
            opts.append(f"credentials={cred}" if share["username"] else "guest")
            versions = [share["version"]] if share["version"] else ["3.1.1", "3.0", "2.1"]
            src = f"//{share['server']}/{share['share']}"
            attempts = [["mount", "-t", "cifs", "-o", ",".join(opts + [f"vers={v}"]), src, mp] for v in versions]
        else:
            src = f"{share['server']}:/{share['share']}"
            opts = [ro, "hard", "timeo=600", "retrans=3", "noatime"]
            versions = [share["version"]] if share["version"] else ["4.2", "4.1", "3"]
            attempts = [["mount", "-t", "nfs", "-o", ",".join(opts + [f"nfsvers={v}"]), src, mp] for v in versions]
        last = ""
        for cmd in attempts:
            code, _, err = run(cmd, timeout=60)
            if code == 0:
                self.errors.pop(sid, None)
                log.info("Netzwerkfreigabe %s eingehängt unter %s [%s]", src, mp, ro)
                return mp
            last = err.strip()
        if "Permission denied" in last or "error(13)" in last:
            last += " – Benutzername/Passwort prüfen"
        elif "No route" in last or "Host is down" in last or "error(112)" in last:
            last += " – Server nicht erreichbar"
        self.errors[sid] = last or "Einhängen fehlgeschlagen"
        try:
            os.rmdir(mp)
        except OSError:
            pass
        raise RuntimeError(self.errors[sid])

    def unmount(self, sid):
        share = self.get(sid)
        if not share:
            raise ValueError("Freigabe nicht gefunden")
        if config.DEV:
            return
        mp = self.mountpoint(share)
        code, _, err = run(["umount", mp], timeout=60)
        if code != 0:
            raise RuntimeError(err.strip() or "Aushängen fehlgeschlagen (wird noch benutzt?)")
        try:
            os.rmdir(mp)
        except OSError:
            pass

    def auto_mount(self):
        for s in list(self.list()):
            if s.get("automount", True) and not self.is_mounted(s):
                try:
                    self.mount(s["id"])
                except Exception as err:  # noqa: BLE001
                    log.warning("Netzwerkfreigabe %s: %s", s["name"], err)

    # ------------------------------------------------------------ Als „Platte“ darstellen
    def as_disks(self, usage_fn, poolparts_fn):
        out = []
        for s in self.list():
            mp = self.mountpoint(s)
            mounted = self.is_mounted(s)
            u = usage_fn(mp) if mounted else None
            src = f"//{s['server']}/{s['share']}" if s["kind"] == "smb" else f"{s['server']}:/{s['share']}"
            out.append({
                "name": s["name"], "path": src, "model": s["name"], "serial": self.uuid(s),
                "size": (u or {}).get("total", 0), "transport": s["kind"], "rotational": False,
                "system": False, "network": True, "share": {k: v for k, v in s.items()},
                "partitions": [{
                    "name": s["name"], "path": src, "uuid": self.uuid(s), "fstype": "cifs" if s["kind"] == "smb" else "nfs",
                    "label": s["name"], "size": (u or {}).get("total", 0),
                    "mountpoint": mp if mounted else None, "mount_ro": None,
                    "poolparts": poolparts_fn(mp) if mounted else [], "usage": u,
                    "error": self.errors.get(s["id"]),
                }],
            })
        return out
