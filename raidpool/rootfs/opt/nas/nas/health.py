"""Gesundheit: Plattenausfall im Betrieb, Meldungen in HA, Dateisystem-Prüfung (chkdsk), SMART-Test, USV."""
import collections
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request

from . import config
from .util import read_mounts, run

log = logging.getLogger("nas")

DEFAULTS = {
    "notify": True,
    "dirty_policy": "readonly",      # readonly | repair | mount
    "ups_entity": "",
    "ups_low_battery_pct": 20,
    "ups_battery_entity": "",
    "ups_unmount_on_low": False,
}

ON_BATTERY = ("ob", "on battery", "onbatt", "battery", "discharging", "batterie")
LOW_BATTERY = ("lb", "low battery")


def ha_call(method, path, body=None, timeout=10):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token or config.DEV:
        return None
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://supervisor/core/api/" + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


class Health:
    def __init__(self, options, settings, disks, pools, rep, dupes):
        self.options = options
        self.settings = settings
        self.disks = disks
        self.pools = pools
        self.rep = rep
        self.dupes = dupes
        self.stop = threading.Event()
        self.failed = {}                 # (pool, uuid) -> seit
        self.events = collections.deque(maxlen=100)
        self.ups = {"state": None, "on_battery": False, "battery": None, "paused": False, "unmounted": False}
        self.check = None                # laufende Dateisystem-Prüfung
        self.check_log = collections.deque(maxlen=2000)
        self.smart_tests = {}            # device -> Status
        self.disks.dirty_policy = self.cfg()["dirty_policy"]

    # ------------------------------------------------------------ Einstellungen
    def cfg(self):
        c = dict(DEFAULTS)
        c.update(self.settings.data.get("health") or {})
        return c

    def set_cfg(self, values):
        with self.settings.lock:
            cur = self.settings.data.setdefault("health", {})
            for k, v in values.items():
                if k in DEFAULTS:
                    cur[k] = v
            self.settings.save()
        self.disks.dirty_policy = self.cfg()["dirty_policy"]
        return self.cfg()

    # ------------------------------------------------------------ Meldungen
    def notify(self, title, message, nid=None, level="warning"):
        self.events.appendleft({"t": int(time.time()), "title": title, "message": message, "level": level})
        (log.warning if level != "info" else log.info)("%s: %s", title, message)
        if not self.cfg()["notify"]:
            return
        try:
            ha_call("POST", "services/persistent_notification/create",
                    {"title": f"RAIDPool: {title}", "message": message,
                     "notification_id": f"raidpool_{nid or int(time.time())}"})
        except Exception as err:  # noqa: BLE001
            log.debug("Meldung an HA fehlgeschlagen: %s", err)

    # ------------------------------------------------------------ Plattenausfall erkennen
    @staticmethod
    def _alive(path, timeout=8):
        """Ordner lesbar? Läuft in eigenem Thread, weil tote Platten Zugriffe hängen lassen können."""
        res = {}

        def probe():
            try:
                os.listdir(path)
                os.statvfs(path)
                res["ok"] = True
            except OSError as err:
                res["err"] = err
        t = threading.Thread(target=probe, daemon=True)
        t.start()
        t.join(timeout)
        return res.get("ok", False)

    def monitor_loop(self):
        while not self.stop.wait(20):
            try:
                self._check_members()
                self._check_ups()
            except Exception:  # noqa: BLE001
                log.exception("Überwachung")

    def _label(self, uuid):
        disk, part = self.disks.partition(uuid)
        return (part or {}).get("label") or (disk or {}).get("model") or uuid[:8]

    def _check_members(self):
        mounts = read_mounts()
        for pool in list(self.settings.data["pools"]):
            if not self.pools.is_mounted(pool):
                continue
            for m in pool["members"]:
                uuid = m["uuid"]
                if uuid in self.disks.busy:
                    continue
                key = (pool["id"], uuid)
                mp = os.path.join(config.DISK_MNT, uuid)
                branch = os.path.join(mp, m["poolpart"]) if m.get("poolpart") not in (None, "", ".") else mp
                mounted = config.DEV or mp in mounts
                ok = mounted and self._alive(branch)
                if not ok and key not in self.failed:
                    self._on_failure(pool, uuid, mp)
                elif key in self.failed:
                    if ok:
                        self._on_return(pool, uuid)
                    elif not mounted:
                        self._try_remount(pool, uuid)

    def _on_failure(self, pool, uuid, mp):
        self.failed[(pool["id"], uuid)] = int(time.time())
        name = self._label(uuid)
        self.pools.branch_remove(pool, uuid)
        if not config.DEV and mp in read_mounts():
            run(["umount", "-l", mp])          # tote Platte freigeben, damit nichts hängt
        self.disks.refresh()
        msg = (f"Platte **{name}** im Pool {pool['name']} ist nicht mehr erreichbar. Der Pool läuft mit den "
               f"übrigen Platten weiter.")
        if self.rep.writable() and self.rep.cfg()["enabled"]:
            msg += " Dateien, die dadurch unter ihr Duplizierungs-Soll fallen, werden jetzt neu kopiert."
            try:
                self.rep.start_job("abgleich", pool_id=pool["id"])
            except ValueError as err:
                log.info("Abgleich nach Ausfall später: %s", err)
        msg += " Bitte Platte prüfen oder im Pool ersetzen."
        self.notify("Platte ausgefallen", msg, nid=f"fail_{uuid}")

    def _try_remount(self, pool, uuid):
        self.disks.refresh()
        _, part = self.disks.partition(uuid)
        if part and not part["mountpoint"]:
            try:
                self.disks.mount(uuid)
            except Exception:  # noqa: BLE001
                pass

    def _on_return(self, pool, uuid):
        self.failed.pop((pool["id"], uuid), None)
        self.disks.refresh()
        self.pools.branch_add(pool, uuid)
        self.notify("Platte wieder da", f"Platte **{self._label(uuid)}** im Pool {pool['name']} ist wieder "
                    f"verfügbar und wurde eingebunden.", nid=f"fail_{uuid}", level="info")
        try:
            if self.rep.writable():
                self.rep.start_job("abgleich", pool_id=pool["id"])
        except ValueError:
            pass

    # ------------------------------------------------------------ USV (NUT-Integration in HA)
    def _state(self, entity):
        try:
            return ha_call("GET", f"states/{entity}")
        except Exception:  # noqa: BLE001
            return None

    def _check_ups(self):
        c = self.cfg()
        if not c["ups_entity"]:
            self.ups.update(state=None, on_battery=False)
            self._resume_from_ups()
            return
        st = self._state(c["ups_entity"])
        if not st:
            return
        state = str(st.get("state", ""))
        low = state.lower()
        on_batt = any(w == low or low.startswith(w + " ") or f" {w}" in f" {low}" for w in ON_BATTERY)
        is_low = any(w in low.split() or w == low for w in LOW_BATTERY)
        battery = None
        if c["ups_battery_entity"]:
            b = self._state(c["ups_battery_entity"])
            try:
                battery = float(b["state"]) if b else None
            except (TypeError, ValueError):
                battery = None
        if battery is not None and battery <= c["ups_low_battery_pct"]:
            is_low = is_low or on_batt
        self.ups.update(state=state, on_battery=on_batt, battery=battery)
        if on_batt and not self.ups["paused"]:
            self.ups["paused"] = True
            self.rep.paused_reason = "USV im Batteriebetrieb"
            self.dupes.paused_reason = "USV im Batteriebetrieb"
            self.dupes.cancel.set()
            self.rep.cancel.set()
            run(["sync"])
            self.notify("Stromausfall", "USV läuft auf Batterie – Duplizierung, Duplikatsuche und Ausbalancieren "
                        "sind pausiert, Daten wurden auf die Platten geschrieben.", nid="ups")
        if on_batt and is_low and c["ups_unmount_on_low"] and not self.ups["unmounted"]:
            self.ups["unmounted"] = True
            self.notify("Akku fast leer", "Pools werden ausgehängt, damit beim Abschalten nichts beschädigt wird.",
                        nid="ups")
            self.unmount_all()
        if not on_batt:
            self._resume_from_ups()

    def _resume_from_ups(self):
        if self.ups["paused"]:
            self.ups["paused"] = False
            self.rep.paused_reason = None
            self.dupes.paused_reason = None
            self.notify("Strom wieder da", "Duplizierung läuft wieder.", nid="ups", level="info")
        if self.ups["unmounted"]:
            self.ups["unmounted"] = False
            self.disks.auto_mount()
            self.pools.mount_all()

    def unmount_all(self):
        run(["sync"])
        for pool in list(self.settings.data["pools"]):
            try:
                self.pools.unmount(pool)
            except Exception:  # noqa: BLE001
                run(["umount", "-l", self.pools.mountpoint(pool)])
        for d in self.disks.disks:
            for p in d["partitions"]:
                if p["mountpoint"] and not d.get("network"):
                    try:
                        self.disks.unmount(p["uuid"])
                    except Exception:  # noqa: BLE001
                        pass

    def ups_candidates(self):
        try:
            states = ha_call("GET", "states") or []
        except Exception as err:  # noqa: BLE001
            raise RuntimeError(f"Sensoren aus HA nicht lesbar: {err}") from err
        if config.DEV:
            states = [{"entity_id": "sensor.usv_status", "state": "Online", "attributes": {"friendly_name": "USV Status"}},
                      {"entity_id": "sensor.usv_battery_charge", "state": "100", "attributes": {"friendly_name": "USV Akku", "unit_of_measurement": "%"}}]
        status, battery = [], []
        for s in states:
            eid = s.get("entity_id", "")
            if not eid.startswith("sensor."):
                continue
            name = (s.get("attributes") or {}).get("friendly_name", eid)
            hay = (eid + " " + name).lower()
            if any(k in hay for k in ("ups", "usv", "nut")):
                item = {"entity_id": eid, "name": name, "state": s.get("state")}
                if (s.get("attributes") or {}).get("unit_of_measurement") == "%" or "battery" in hay or "akku" in hay:
                    battery.append(item)
                else:
                    status.append(item)
        return {"status": status, "battery": battery}

    # ------------------------------------------------------------ Dateisystem-Prüfung (chkdsk)
    def check_tools(self, fstype, repair):
        nt = self.disks.ntfsck_available()
        if fstype == "ntfs":
            if nt:
                return (["ntfsck", "-a"] if repair else ["ntfsck", "-n"]), "ntfsck"
            return (["ntfsfix", "-d"] if repair else ["ntfsfix", "-n"]), "ntfsfix"
        if fstype == "ext4":
            return (["e2fsck", "-f", "-p"] if repair else ["e2fsck", "-f", "-n"]), "e2fsck"
        if fstype == "xfs":
            return (["xfs_repair"] if repair else ["xfs_repair", "-n"]), "xfs_repair"
        if fstype == "btrfs":
            if repair:
                raise ValueError("btrfs wird nur geprüft – Reparatur bitte mit „btrfs scrub“ bei eingehängter Platte")
            return ["btrfs", "check", "--readonly"], "btrfs check"
        if fstype == "exfat":
            return (["fsck.exfat", "-y"] if repair else ["fsck.exfat", "-n"]), "fsck.exfat"
        raise ValueError(f"Prüfung für Dateisystem '{fstype}' nicht möglich")

    def start_check(self, uuid, repair=False):
        if self.check and self.check.get("running"):
            raise ValueError("Es läuft bereits eine Prüfung")
        disk, part = self.disks.partition(uuid)
        if not part or (disk or {}).get("network"):
            raise ValueError("Platte nicht gefunden")
        if disk["system"]:
            raise ValueError("Die Systemplatte wird nicht geprüft")
        if repair and self.options["read_only"]:
            raise ValueError("Reparieren nur mit ausgeschaltetem 'read_only'")
        cmd, tool = self.check_tools(part["fstype"], repair)
        self.check = {"uuid": uuid, "label": self._label(uuid), "repair": repair, "tool": tool, "running": True,
                      "started": int(time.time()), "phase": "Vorbereiten", "result": None}
        self.check_log.clear()
        threading.Thread(target=self._run_check, args=(uuid, part, cmd), daemon=True, name="fsck").start()

    def _say(self, line):
        self.check_log.append(line.rstrip())

    def _run_check(self, uuid, part, cmd):
        c = self.check
        pools = [p for p in self.settings.data["pools"] if any(m["uuid"] == uuid for m in p["members"])]
        self.disks.busy.add(uuid)
        try:
            c["phase"] = "Platte aus dem Pool lösen"
            for p in pools:
                self.pools.branch_remove(p, uuid)
                self._say(f"Platte vorübergehend aus Pool {p['name']} gelöst")
            if part["mountpoint"]:
                c["phase"] = "Aushängen"
                if not config.DEV:
                    code, _, err = run(["umount", part["mountpoint"]], timeout=120)
                    if code != 0:
                        raise RuntimeError(f"Aushängen nicht möglich (wird noch benutzt?): {err.strip()}")
                self._say("Platte ausgehängt")
            c["phase"] = f"{c['tool']} läuft"
            full = cmd + [part["path"]]
            self._say("$ " + " ".join(full))
            if config.DEV:
                for line in ("Prüfe Dateisystem …", "Phase 1: MFT", "Phase 2: Verzeichnisse",
                             "Phase 3: Cluster-Bitmap", "Keine Fehler gefunden."):
                    time.sleep(0.5)
                    self._say(line)
                code = 0
            else:
                proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                for line in proc.stdout:
                    self._say(line)
                code = proc.wait()
            c["exit_code"] = code
            ok_codes = {"e2fsck": (0, 1), "ntfsck": (0, 1) if c["repair"] else (0,), "fsck.exfat": (0, 1)}.get(c["tool"], (0,))
            if code in ok_codes:
                c["result"] = "repariert" if (c["repair"] and code == 1) else "ok"
                if c["repair"] or code == 0:
                    self.disks.dirty.pop(uuid, None)
                    self.disks.forced_ro.discard(uuid)
            else:
                c["result"] = "fehler gefunden" if not c["repair"] else "reparatur unvollständig"
        except Exception as err:  # noqa: BLE001
            c["result"] = "abgebrochen"
            c["error"] = str(err)
            self._say(f"FEHLER: {err}")
        finally:
            self.disks.busy.discard(uuid)
            c["phase"] = "Wieder einhängen"
            try:
                self.disks.refresh()
                self.disks.mount(uuid)
                self.disks.refresh()
                for p in pools:
                    self.pools.branch_add(p, uuid)
                    self._say(f"Wieder in Pool {p['name']} eingebunden")
            except Exception as err:  # noqa: BLE001
                self._say(f"Wieder einhängen fehlgeschlagen: {err}")
            c["running"] = False
            c["finished"] = int(time.time())
            c["phase"] = "Fertig"
            self.notify("Dateisystem-Prüfung", f"{c['label']}: {c['result']}", nid=f"fsck_{uuid}",
                        level="info" if c["result"] in ("ok", "repariert") else "warning")

    # ------------------------------------------------------------ SMART-Oberflächentest (wie chkdsk /r)
    def start_smart_test(self, device, kind="long"):
        if config.DEV:
            self.smart_tests[device] = {"running": True, "remaining": 90, "kind": kind, "started": int(time.time())}
            return
        code, out, err = run(["smartctl", "-t", kind, device], timeout=30)
        if code not in (0, 4):
            code, out, err = run(["smartctl", "-d", "sat", "-t", kind, device], timeout=30)
        if code not in (0, 4):
            raise RuntimeError((out + err).strip().splitlines()[-1] if (out + err).strip() else "Test nicht startbar")
        self.smart_tests[device] = {"running": True, "remaining": 100, "kind": kind, "started": int(time.time())}

    def smart_test_status(self, device):
        st = self.smart_tests.get(device)
        if config.DEV:
            if st and st["running"]:
                st["remaining"] = max(0, st["remaining"] - 10)
                st["running"] = st["remaining"] > 0
                st["result"] = None if st["running"] else "Ohne Fehler abgeschlossen"
            return st
        code, out, _ = run(["smartctl", "-j", "-c", "-l", "selftest", device], timeout=30)
        try:
            data = json.loads(out or "{}")
        except json.JSONDecodeError:
            return st
        status = ((data.get("ata_smart_data") or {}).get("self_test") or {}).get("status") or {}
        remaining = status.get("remaining_percent")
        table = ((data.get("ata_smart_self_test_log") or {}).get("standard") or {}).get("table") or []
        last = table[0] if table else None
        res = {"running": remaining is not None, "remaining": remaining,
               "result": ((last or {}).get("status") or {}).get("string") if last else None,
               "lifetime_hours": (last or {}).get("lifetime_hours")}
        self.smart_tests[device] = dict(st or {}, **res)
        return self.smart_tests[device]

    # ------------------------------------------------------------ Status
    def status(self):
        return {"config": self.cfg(), "failed": [{"pool": p, "uuid": u, "since": t, "label": self._label(u)}
                                                 for (p, u), t in self.failed.items()],
                "events": list(self.events)[:30], "ups": self.ups, "check": self.check,
                "check_log": list(self.check_log)[-400:], "ntfsck": self.disks.ntfsck_available(),
                "smart_tests": self.smart_tests}
