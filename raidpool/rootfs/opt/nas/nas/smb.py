"""SMB-Freigaben (Samba): ganze Pools oder einzelne Ordner, Benutzer mit eigenem SMB-Passwort.

Benutzernamen können aus Home Assistant übernommen werden. Das SMB-Passwort wird separat
vergeben, weil HA Passwörter nur als Hash speichert, den Samba nicht verwenden kann.
"""
import base64
import json
import logging
import os
import re
import socket
import struct
import subprocess
import tempfile
import threading
import time
import uuid as uuidlib

from . import config
from .util import run

log = logging.getLogger("nas")

SAMBA_DIR = config.DATA_DIR + "/samba"
CONF = SAMBA_DIR + "/smb.conf"
RECYCLE = ".nas-recycle"
RESERVED = {"global", "homes", "printers", "print$", "ipc$"}
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
SHARE_RE = re.compile(r'^[^\\/:*?"<>|\[\];=,+]{1,64}$')

DEFAULT_SMB = {"enabled": True, "workgroup": "WORKGROUP", "server_name": "", "wsdd": True,
               "netbios": False, "min_protocol": "SMB2_10",
               "aliases": [],          # weitere Servernamen, z. B. „SERVER“ (alter Windows-Server)
               "extra_ips": []}        # weitere IP-Adressen, z. B. die alte IP des Windows-Servers
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,14}$")
IP_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(/(\d{1,2}))?$")
ACCESS = ("none", "ro", "rw")


# ---------------------------------------------------------------- HA-Benutzer (WebSocket, ohne Zusatzpakete)
def _ws_frame(payload):
    data = payload.encode()
    head = bytearray([0x81])
    n = len(data)
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    return bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def _ws_recv(sock_file):
    msg = b""
    while True:
        b1, b2 = sock_file.read(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", sock_file.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", sock_file.read(8))[0]
        if b2 & 0x80:
            sock_file.read(4)
        msg += sock_file.read(n)
        if b1 & 0x80:
            return json.loads(msg.decode())


def ha_users():
    """Benutzer aus Home Assistant (config/auth/list über die Supervisor-Core-Proxy-WebSocket)."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if config.DEV:
        return [{"username": "ingo", "name": "Ingo"}, {"username": "anna", "name": "Anna"},
                {"username": "kiosk", "name": "Wandpanel"}]
    if not token:
        return []
    s = socket.create_connection(("supervisor", 80), timeout=10)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET /core/websocket HTTP/1.1\r\nHost: supervisor\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   f"Authorization: Bearer {token}\r\n\r\n").encode())
        f = s.makefile("rb")
        line = f.readline()
        if b" 101 " not in line:
            raise RuntimeError(f"WebSocket abgelehnt: {line!r}")
        while f.readline() not in (b"\r\n", b""):
            pass
        _ws_recv(f)                                             # auth_required
        s.sendall(_ws_frame(json.dumps({"type": "auth", "access_token": token})))
        if _ws_recv(f).get("type") != "auth_ok":
            raise RuntimeError("Anmeldung an Home Assistant fehlgeschlagen")
        s.sendall(_ws_frame(json.dumps({"id": 1, "type": "config/auth/list"})))
        res = _ws_recv(f)
        users = []
        for u in res.get("result") or []:
            if u.get("system_generated") or not u.get("is_active", True) or not u.get("username"):
                continue
            users.append({"username": u["username"].lower(), "name": u.get("name") or u["username"]})
        return users
    finally:
        s.close()


# ---------------------------------------------------------------- Verwaltung
class SambaManager:
    def __init__(self, options, settings, pools):
        self.options = options
        self.settings = settings
        self.pools = pools
        self.proc = None
        self.wsdd = None
        self.error = None
        self.lock = threading.RLock()
        self.stop_evt = threading.Event()
        self.restarts = 0
        self.ip_state = {}     # ip -> {"active": bool, "error": str}

    # ------------------------------------------------------------ Daten
    def cfg(self):
        c = dict(DEFAULT_SMB)
        c.update(self.settings.data.get("smb") or {})
        return c

    def users(self):
        return self.settings.data.setdefault("smb_users", [])

    def shares(self):
        return self.settings.data.setdefault("smb_shares", [])

    def server_name(self):
        return (self.cfg()["server_name"] or socket.gethostname().split(".")[0] or "homeassistant")[:15]

    def set_cfg(self, values):
        if "aliases" in values:
            al = []
            for a in values["aliases"]:
                a = a.strip().upper()
                if not a:
                    continue
                if not NAME_RE.match(a):
                    raise ValueError(f"Ungültiger Servername '{a}' (max. 15 Zeichen, Buchstaben/Ziffern/-)")
                if a not in al and a != self.server_name().upper():
                    al.append(a)
            values["aliases"] = al
        if "extra_ips" in values:
            ips = []
            for ip in values["extra_ips"]:
                ip = ip.strip()
                m = IP_RE.match(ip)
                if not m or any(int(x) > 255 for x in m.group(1, 2, 3, 4)) or (m.group(6) and int(m.group(6)) > 32):
                    raise ValueError(f"Ungültige IP-Adresse '{ip}' (z. B. 192.168.0.10 oder 192.168.0.10/24)")
                if ip not in ips:
                    ips.append(ip)
            values["extra_ips"] = ips
        old_ips = set(self.cfg()["extra_ips"])
        with self.settings.lock:
            cur = self.settings.data.setdefault("smb", {})
            for k in DEFAULT_SMB:
                if k in values:
                    cur[k] = values[k]
            self.settings.save()
        for ip in old_ips - set(self.cfg()["extra_ips"]):
            self.remove_ip(ip)
        self.apply_ips()
        self.apply(restart=True)
        return self.cfg()

    # ------------------------------------------------------------ Namen im Netzwerk
    @staticmethod
    def own_ips():
        code, out, _ = run(["ip", "-4", "-o", "addr", "show"], timeout=10)
        return {line.split()[3].split("/")[0] for line in out.splitlines() if len(line.split()) > 3} if code == 0 else set()

    def check_name(self, name):
        """Antwortet im Netzwerk schon jemand anderes auf diesen NetBIOS-Namen? -> Liste fremder IPs."""
        if config.DEV:
            return ["192.168.0.10"] if name.upper() == "BELEGT" else []
        code, out, _ = run(["nmblookup", name], timeout=15)
        ips = set(re.findall(r"^(\d+\.\d+\.\d+\.\d+)\s+\S+<00>", out, re.M))
        return sorted(ips - self.own_ips())

    # ------------------------------------------------------------ Zusätzliche IP-Adressen
    @staticmethod
    def default_iface():
        code, out, _ = run(["ip", "-4", "route", "show", "default"], timeout=10)
        m = re.search(r"\bdev\s+(\S+)", out)
        return m.group(1) if m else None

    @staticmethod
    def _prefix_of(iface):
        code, out, _ = run(["ip", "-4", "-o", "addr", "show", "dev", iface], timeout=10)
        m = re.search(r"inet\s+\S+/(\d+)", out)
        return m.group(1) if m else "24"

    def apply_ips(self):
        if config.DEV:
            for ip in self.cfg()["extra_ips"]:
                self.ip_state[ip] = {"active": True, "error": None, "iface": "eth0"}
            return
        iface = self.default_iface()
        for ip in self.cfg()["extra_ips"]:
            st = self.ip_state.setdefault(ip, {"active": False, "error": None})
            if not iface:
                st["error"] = "Keine Netzwerkschnittstelle gefunden"
                continue
            addr = ip.split("/")[0]
            if addr in self.own_ips():
                st.update(active=True, error=None, iface=iface)
                continue
            # Ist die Adresse schon von einem anderen Gerät belegt (z. B. alte Windows-VM noch an)?
            code, _, _ = run(["arping", "-D", "-c", "2", "-w", "3", "-I", iface, addr], timeout=10)
            if code == 1:
                st.update(active=False, error="Adresse wird von einem anderen Gerät verwendet – alter Server noch an?")
                continue
            cidr = ip if "/" in ip else f"{ip}/{self._prefix_of(iface)}"
            code, _, err = run(["ip", "addr", "add", cidr, "dev", iface, "label", f"{iface}:rp"[:15]], timeout=10)
            if code != 0 and "File exists" not in err:
                st.update(active=False, error=err.strip() or "Hinzufügen fehlgeschlagen")
                continue
            run(["arping", "-U", "-c", "2", "-I", iface, addr], timeout=10)   # Geräte über neue Adresse informieren
            st.update(active=True, error=None, iface=iface, cidr=cidr)
            log.info("Zusätzliche IP %s auf %s aktiv", cidr, iface)

    def remove_ip(self, ip):
        st = self.ip_state.pop(ip, None)
        if config.DEV or not st or not st.get("cidr"):
            return
        run(["ip", "addr", "del", st["cidr"], "dev", st.get("iface") or self.default_iface() or "eth0"], timeout=10)

    def remove_all_ips(self):
        for ip in list(self.ip_state):
            self.remove_ip(ip)

    # ------------------------------------------------------------ Benutzer
    def _unix_user(self, name):
        if config.DEV:
            return
        if run(["id", "-u", name])[0] != 0:
            run(["useradd", "-M", "-N", "-s", "/usr/sbin/nologin", name], check=True)

    def _check_logins(self, name, logins):
        clean = []
        taken = {u["name"].lower() for u in self.users() if u["name"] != name}
        for u in self.users():
            if u["name"] != name:
                taken |= {x.lower() for x in u.get("logins") or []}
        for x in logins or []:
            x = x.strip()
            if not x:
                continue
            if any(ch in x for ch in '"=\n'):
                raise ValueError(f"Ungültiger Anmeldename '{x}'")
            if x.lower() in taken:
                raise ValueError(f"Anmeldename '{x}' wird schon von einem anderen Benutzer verwendet")
            clean.append(x)
        return clean

    def set_logins(self, name, logins):
        clean = self._check_logins(name, logins)
        with self.settings.lock:
            for u in self.users():
                if u["name"] == name:
                    u["logins"] = clean
            self.settings.save()
        self.apply()

    def add_user(self, name, password, ha=False, display="", logins=None):
        name = (name or "").strip().lower()
        if not USER_RE.match(name):
            raise ValueError("Benutzername: Kleinbuchstaben, Ziffern, . _ - (max. 32 Zeichen)")
        if any(u["name"] == name for u in self.users()):
            raise ValueError("Benutzer existiert bereits")
        if not password or len(password) < 4:
            raise ValueError("Passwort mit mindestens 4 Zeichen angeben")
        clean = self._check_logins(name, logins)
        self._set_password(name, password)
        with self.settings.lock:
            self.users().append({"name": name, "display": display or name, "ha": bool(ha),
                                 "enabled": True, "created": int(time.time()), "logins": clean})
            self.settings.save()
        self.apply()

    def _set_password(self, name, password):
        self._unix_user(name)
        if config.DEV:
            return
        os.makedirs(SAMBA_DIR, exist_ok=True)
        self.write_conf()
        res = subprocess.run(["smbpasswd", "-c", CONF, "-s", "-a", name], input=f"{password}\n{password}\n",
                             capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "smbpasswd fehlgeschlagen")
        run(["smbpasswd", "-c", CONF, "-e", name])

    def set_password(self, name, password):
        if not any(u["name"] == name for u in self.users()):
            raise ValueError("Benutzer nicht gefunden")
        if not password or len(password) < 4:
            raise ValueError("Passwort mit mindestens 4 Zeichen angeben")
        self._set_password(name, password)
        with self.settings.lock:
            for u in self.users():
                if u["name"] == name:
                    u["password_set"] = int(time.time())
            self.settings.save()

    def set_user_enabled(self, name, enabled):
        with self.settings.lock:
            for u in self.users():
                if u["name"] == name:
                    u["enabled"] = bool(enabled)
            self.settings.save()
        if not config.DEV:
            run(["smbpasswd", "-c", CONF, "-e" if enabled else "-d", name])
        self.apply()

    def remove_user(self, name):
        with self.settings.lock:
            self.settings.data["smb_users"] = [u for u in self.users() if u["name"] != name]
            for s in self.shares():
                s.get("access", {}).pop(name, None)
            self.settings.save()
        if not config.DEV:
            run(["smbpasswd", "-c", CONF, "-x", name])
        self.apply()

    def ha_candidates(self):
        existing = {u["name"] for u in self.users()}
        try:
            return [dict(u, valid=bool(USER_RE.match(u["username"]))) for u in ha_users()
                    if u["username"] not in existing]
        except Exception as err:  # noqa: BLE001
            log.warning("HA-Benutzer nicht lesbar: %s", err)
            raise RuntimeError(f"Benutzer aus Home Assistant nicht lesbar: {err}") from err

    # ------------------------------------------------------------ Freigaben
    def save_share(self, data):
        name = (data.get("name") or "").strip()
        if not SHARE_RE.match(name) or name.lower() in RESERVED:
            raise ValueError("Ungültiger Freigabename")
        pool = self.settings.pool(data.get("pool"))
        if not pool:
            raise ValueError("Pool auswählen")
        path = (data.get("path") or "").strip().strip("/")
        if ".." in path.split("/"):
            raise ValueError("Ungültiger Pfad")
        access = {k: v for k, v in (data.get("access") or {}).items() if v in ACCESS}
        with self.settings.lock:
            sid = data.get("id")
            for s in self.shares():
                if s["name"].lower() == name.lower() and s["id"] != sid:
                    raise ValueError("Es gibt bereits eine Freigabe mit diesem Namen")
            entry = next((s for s in self.shares() if s["id"] == sid), None) if sid else None
            if entry is None:
                entry = {"id": uuidlib.uuid4().hex[:10], "created": int(time.time())}
                self.shares().append(entry)
            entry.update({
                "name": name, "pool": pool["id"], "path": path,
                "comment": (data.get("comment") or "").strip()[:120],
                "enabled": bool(data.get("enabled", True)),
                "browseable": bool(data.get("browseable", True)),
                "guest": data.get("guest") if data.get("guest") in ACCESS else "none",
                "recycle": bool(data.get("recycle", True)),
                "mac": bool(data.get("mac", False)),
                "timemachine": bool(data.get("timemachine", False)),
                "tm_size_gb": int(data.get("tm_size_gb") or 0),
                "access": access,
            })
            self.settings.save()
        target = self.share_path(entry)
        if not self.options["read_only"] and not os.path.isdir(target) and os.path.isdir(self.share_path(dict(entry, path=""))):
            try:
                os.makedirs(target, exist_ok=True)   # Ordner im Pool anlegen, falls es ihn noch nicht gibt
            except OSError as err:
                log.warning("Ordner %s nicht anlegbar: %s", target, err)
        self.apply()
        return entry

    def remove_share(self, sid):
        with self.settings.lock:
            self.settings.data["smb_shares"] = [s for s in self.shares() if s["id"] != sid]
            self.settings.save()
        self.apply()

    def share_path(self, s):
        base = self.pools.mountpoint({"id": s["pool"]})
        return os.path.join(base, s["path"]) if s["path"] else base

    # ------------------------------------------------------------ Übernahme vom alten Server
    def remote_shares(self, host, user, password):
        """Freigaben (und wenn möglich deren Pfade) eines anderen SMB-Servers auslesen.
        Zugangsdaten werden nur für diesen Abruf in einer temporären Datei verwendet und nicht gespeichert."""
        host = host.strip().strip("\\/").split("\\")[0].split("/")[0]
        if not re.match(r"^[A-Za-z0-9._-]+$", host):
            raise ValueError("Ungültiger Servername")
        if config.DEV:
            shares = [{"name": "Filme", "comment": "Filmsammlung", "path": "P:\\Filme"},
                      {"name": "Fotos", "comment": "", "path": "P:\\Fotos"},
                      {"name": "Software", "comment": "", "path": "P:\\Software"},
                      {"name": "Backup", "comment": "Sicherungen", "path": "D:\\Backup"},
                      {"name": "Pool", "comment": "", "path": "P:\\"}]
        else:
            fd, auth = tempfile.mkstemp(prefix="raidpool-auth-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    u = user or "guest"
                    dom = ""
                    if "\\" in u:
                        dom, u = u.split("\\", 1)
                    fh.write(f"username = {u}\npassword = {password or ''}\n")
                    if dom:
                        fh.write(f"domain = {dom}\n")
                code, out, err = run(["smbclient", "-L", f"//{host}", "-A", auth, "-g", "-m", "SMB3"], timeout=30)
                if code != 0:
                    msg = (err or out).strip().splitlines()[-1] if (err or out).strip() else "Verbindung fehlgeschlagen"
                    if "LOGON_FAILURE" in msg:
                        msg = "Anmeldung fehlgeschlagen – Benutzername/Passwort prüfen"
                    raise RuntimeError(msg)
                shares = []
                for line in out.splitlines():
                    parts = line.split("|")
                    if len(parts) >= 2 and parts[0] == "Disk" and not parts[1].endswith("$"):
                        shares.append({"name": parts[1], "comment": parts[2] if len(parts) > 2 else "", "path": ""})
                # Pfade sind nur mit Administratorrechten lesbar
                code, out, _ = run(["rpcclient", "-A", auth, host, "-c", "netshareenumall"], timeout=30)
                if code == 0:
                    cur = None
                    for line in out.splitlines():
                        line = line.strip()
                        if line.startswith("netname:"):
                            cur = line.split(":", 1)[1].strip()
                        elif line.startswith("path:") and cur:
                            for sh in shares:
                                if sh["name"].lower() == cur.lower():
                                    sh["path"] = line.split(":", 1)[1].strip()
            finally:
                try:
                    os.remove(auth)
                except OSError:
                    pass
        existing = {s["name"].lower() for s in self.shares()}
        pools = self.settings.data["pools"]
        for sh in shares:
            sh["exists"] = sh["name"].lower() in existing
            # Laufwerksbuchstaben entfernen: „P:\Filme\HD“ -> „Filme/HD“
            p = re.sub(r"^[A-Za-z]:", "", sh["path"] or "").replace("\\", "/").strip("/")
            guess = p if sh["path"] else sh["name"]
            sh["pool"], sh["target"], sh["found"] = (pools[0]["id"] if pools else None), guess, False
            for pool in pools:
                base = self.pools.mountpoint(pool)
                if (guess == "" and pool) or os.path.isdir(os.path.join(base, guess)) or \
                        self.pools.index.folder(pool["id"], guess):
                    sh["pool"], sh["found"] = pool["id"], True
                    break
        return shares

    def import_shares(self, items, access):
        done, skipped = 0, []
        for it in items:
            if any(s["name"].lower() == it["name"].lower() for s in self.shares()):
                skipped.append(it["name"])
                continue
            self.save_share({"name": it["name"], "pool": it["pool"], "path": it.get("target", ""),
                             "comment": it.get("comment", ""), "access": dict(access or {}),
                             "recycle": True, "browseable": True})
            done += 1
        return {"imported": done, "skipped": skipped}

    # ------------------------------------------------------------ smb.conf
    def render_conf(self):
        c = self.cfg()
        ro_global = self.options["read_only"]
        enabled_users = {u["name"] for u in self.users() if u.get("enabled", True)}
        lines = [
            "# Automatisch erzeugt von HA RAIDPool – Änderungen werden überschrieben",
            "[global]",
            f"   workgroup = {c['workgroup']}",
            f"   netbios name = {self.server_name()}",
            "   server string = HA RAIDPool",
            "   server role = standalone server",
            f"   passdb backend = tdbsam:{SAMBA_DIR}/passdb.tdb",
            f"   private dir = {SAMBA_DIR}/private",
            f"   state directory = {SAMBA_DIR}/state",
            f"   cache directory = {SAMBA_DIR}/cache",
            "   lock directory = /run/samba",
            "   pid directory = /run/samba",
            "   ncalrpc dir = /run/samba/ncalrpc",
            "   map to guest = Bad User",
            "   guest account = nobody",
            f"   server min protocol = {c['min_protocol']}",
            "   smb ports = 445",
            f"   disable netbios = {'no' if c['netbios'] or c['aliases'] else 'yes'}",
            "   load printers = no",
            "   printing = bsd",
            "   printcap name = /dev/null",
            "   disable spoolss = yes",
            "   log level = 1",
            "   unix extensions = no",
            "   ea support = yes",
            "   store dos attributes = no",
            "   fruit:aapl = yes",
            "   fruit:nfs_aces = no",
            "   fruit:copyfile = yes",
            "   fruit:model = MacSamba",
        ]
        if c["aliases"]:
            lines.append(f"   netbios aliases = {' '.join(c['aliases'])}")
        if any(u.get("logins") for u in self.users()):
            lines.append(f"   username map = {SAMBA_DIR}/usermap")
        lines.append("")
        for s in self.shares():
            if not s.get("enabled", True) or not self.settings.pool(s["pool"]):
                continue
            acc = {u: a for u, a in s.get("access", {}).items() if u in enabled_users and a != "none"}
            rw = sorted(u for u, a in acc.items() if a == "rw")
            ro = sorted(u for u, a in acc.items() if a == "ro")
            guest = s.get("guest", "none")
            vfs = []
            if s.get("mac") or s.get("timemachine"):
                vfs += ["catia", "fruit", "streams_xattr"]
            recycle = s.get("recycle") and not s.get("timemachine")   # Time Machine verwaltet selbst
            if recycle:
                vfs.append("recycle")
            share_ro = ro_global
            lines += [f"[{s['name']}]",
                      f"   path = {self.share_path(s)}",
                      f"   comment = {s.get('comment', '')}",
                      f"   browseable = {'yes' if s.get('browseable', True) else 'no'}",
                      "   force user = root",
                      "   force group = root",
                      "   create mask = 0664",
                      "   directory mask = 0775",
                      "   veto files = /.nas-trash/.covefs/",
                      f"   hide files = /{RECYCLE}/"]
            if guest == "none":
                # nur eingetragene Benutzer; „ro“ lesen, „rw“ lesen + schreiben
                users = rw + ro
                lines.append(f"   valid users = {' '.join(users) if users else 'root'}")
                lines.append(f"   read only = {'yes' if share_ro else 'no'}")
                if not share_ro and ro:
                    lines.append(f"   read list = {' '.join(ro)}")
            elif guest == "ro":
                # jeder darf lesen, schreiben nur Benutzer mit „rw“
                lines += ["   guest ok = yes", "   read only = yes"]
                if not share_ro and rw:
                    lines.append(f"   write list = {' '.join(rw)}")
            else:
                # jeder darf lesen und schreiben (außer Benutzer, die ausdrücklich nur „ro“ haben)
                lines += ["   guest ok = yes", f"   read only = {'yes' if share_ro else 'no'}"]
                if not share_ro and ro:
                    lines.append(f"   read list = {' '.join(ro)}")
            if vfs:
                lines.append(f"   vfs objects = {' '.join(vfs)}")
            if recycle:
                lines += [f"   recycle:repository = {RECYCLE}/%U", "   recycle:keeptree = yes",
                          "   recycle:versions = yes", "   recycle:touch = yes",
                          "   recycle:exclude = *.tmp,*.temp,~$*,.DS_Store,Thumbs.db"]
            if s.get("mac") or s.get("timemachine"):
                lines += ["   fruit:metadata = stream", "   fruit:resource = stream",
                          "   fruit:posix_rename = yes", "   fruit:veto_appledouble = no",
                          "   fruit:wipe_intentionally_left_blank_rfork = yes",
                          "   fruit:delete_empty_adfiles = yes"]
            if s.get("timemachine"):
                lines.append("   fruit:time machine = yes")
                if s.get("tm_size_gb"):
                    lines.append(f"   fruit:time machine max size = {int(s['tm_size_gb'])}G")
            lines.append("")
        return "\n".join(lines)

    def write_usermap(self):
        rows = []
        for u in self.users():
            logins = [x for x in u.get("logins") or [] if x]
            if logins:
                rows.append(f"{u['name']} = " + " ".join(f'"{x}"' if " " in x else x for x in logins))
        os.makedirs(SAMBA_DIR, exist_ok=True)
        with open(SAMBA_DIR + "/usermap", "w", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + ("\n" if rows else ""))

    def write_conf(self):
        self.write_usermap()
        os.makedirs(SAMBA_DIR + "/private", exist_ok=True)
        os.makedirs(SAMBA_DIR + "/state", exist_ok=True)
        os.makedirs(SAMBA_DIR + "/cache", exist_ok=True)
        if not config.DEV:
            os.makedirs("/run/samba/ncalrpc", exist_ok=True)
        text = self.render_conf()
        tmp = CONF + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, CONF)
        return text

    # ------------------------------------------------------------ Prozesse
    @staticmethod
    def _port_busy(port=445):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True
        finally:
            s.close()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        with self.lock:
            if not self.cfg()["enabled"]:
                self.error = None
                return
            self.write_conf()
            if config.DEV or self.running():
                return
            for u in self.users():
                try:
                    self._unix_user(u["name"])
                except Exception as err:  # noqa: BLE001
                    log.warning("Benutzer %s: %s", u["name"], err)
            if self._port_busy(445):
                self.error = ("Port 445 ist belegt – läuft das offizielle Samba-Add-on? "
                              "Bitte dort stoppen, dann hier „Starten“.")
                log.error(self.error)
                return
            self.proc = subprocess.Popen(["smbd", "--foreground", "--no-process-group", "--debug-stdout",
                                          "-s", CONF], stdout=None, stderr=None)
            self.error = None
            log.info("Samba gestartet (PID %s)", self.proc.pid)
            if self.cfg()["netbios"] or self.cfg()["aliases"]:
                subprocess.Popen(["nmbd", "--foreground", "--no-process-group", "--debug-stdout", "-s", CONF])
            if self.cfg()["wsdd"] and self.wsdd is None:
                for exe in ("wsdd", "wsdd2"):
                    try:
                        args = [exe, "--shortlog", "-n", self.server_name(), "-w", self.cfg()["workgroup"]] \
                            if exe == "wsdd" else [exe, "-N", self.server_name(), "-G", self.cfg()["workgroup"]]
                        self.wsdd = subprocess.Popen(args)
                        break
                    except FileNotFoundError:
                        continue

    def stop(self):
        with self.lock:
            for p in (self.proc, self.wsdd):
                if p and p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(10)
                    except subprocess.TimeoutExpired:
                        p.kill()
            self.proc = None
            self.wsdd = None
            run(["pkill", "-x", "nmbd"])

    def shutdown(self):
        self.stop()
        self.remove_all_ips()

    def apply(self, restart=False):
        """Konfiguration schreiben und Samba neu laden (Verbindungen bleiben bestehen)."""
        self.write_conf()
        if config.DEV:
            return
        if not self.cfg()["enabled"]:
            self.stop()
            return
        if restart or not self.running():
            self.stop()
            self.start()
        else:
            run(["smbcontrol", "-s", CONF, "smbd", "reload-config"])

    def watchdog_loop(self):
        while not self.stop_evt.wait(15):
            if self.cfg()["extra_ips"] and self.cfg()["enabled"]:
                try:
                    self.apply_ips()      # z. B. nach Neuverbindung des Netzwerks erneut setzen
                except Exception as err:  # noqa: BLE001
                    log.warning("IP-Adressen: %s", err)
            if config.DEV or not self.cfg()["enabled"] or self.proc is None:
                continue
            if self.proc.poll() is not None and self.restarts < 5:
                self.restarts += 1
                log.warning("Samba beendet (Code %s) – Neustart %d/5", self.proc.returncode, self.restarts)
                self.proc = None
                self.start()

    def sessions(self):
        if config.DEV or not self.running():
            return []
        code, out, _ = run(["smbstatus", "-s", CONF, "--json"], timeout=10)
        if code != 0:
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []
        res = []
        tcons = data.get("tcons") or {}
        for s in (data.get("sessions") or {}).values():
            shares = sorted({t.get("service") for t in tcons.values()
                             if str(t.get("session_id")) == str(s.get("session_id"))})
            res.append({"user": s.get("username"), "host": s.get("remote_machine") or s.get("hostname"),
                        "protocol": s.get("session_dialect"), "shares": shares,
                        "encrypted": bool((s.get("encryption") or {}).get("cipher"))})
        return res

    def status(self):
        c = self.cfg()
        return {
            "config": c, "server_name": self.server_name(), "running": config.DEV or self.running(),
            "dev": config.DEV, "error": self.error, "read_only": self.options["read_only"],
            "users": self.users(), "shares": [dict(s, unc=f"\\\\{self.server_name()}\\{s['name']}",
                                                   pool_name=(self.settings.pool(s["pool"]) or {}).get("name"),
                                                   path_ok=config.DEV or os.path.isdir(self.share_path(s)))
                                              for s in self.shares()],
            "sessions": self.sessions(),
            "ips": [dict(self.ip_state.get(ip, {"active": False, "error": None}), ip=ip) for ip in c["extra_ips"]],
            "own_ips": sorted(self.own_ips()) if not config.DEV else ["192.168.0.50"],
        }
