"""Pools: mehrere Platten (bzw. deren PoolPart-Ordner) per mergerfs zu einem Laufwerk zusammenfassen."""
import logging
import os
import threading
import uuid as uuidlib

from . import config
from .disks import _usage
from .util import read_mounts, run, slug

log = logging.getLogger("nas")


class PoolManager:
    def __init__(self, options, settings, disks, index):
        self.options = options
        self.settings = settings
        self.disks = disks
        self.index = index
        self.errors = {}
        self.scan_threads = {}

    # ------------------------------------------------------------ Hilfen
    def mountpoint(self, pool):
        return os.path.join(config.POOL_MNT, pool["id"])

    def branches(self, pool):
        """(disk_uuid, pfad, vorhanden) je Mitglied."""
        res = []
        for m in pool["members"]:
            base = os.path.join(config.DISK_MNT, m["uuid"])
            path = os.path.join(base, m["poolpart"]) if m.get("poolpart") not in (None, "", ".") else base
            res.append((m["uuid"], path, os.path.isdir(path)))
        return res

    def is_mounted(self, pool):
        mp = self.mountpoint(pool)
        if config.DEV:
            return True
        return mp in read_mounts()

    # ------------------------------------------------------------ Anlegen / Löschen
    def create(self, name, members, default_target=1, adopt=False):
        """members: [{'uuid':..., 'poolpart': 'PoolPart.xxx' | ''}]"""
        if not members:
            raise ValueError("Mindestens eine Platte auswählen")
        with self.settings.lock:
            pid = slug(name)
            existing = {p["id"] for p in self.settings.data["pools"]}
            base, n = pid, 2
            while pid in existing:
                pid, n = f"{base}_{n}", n + 1
            used = {(m["uuid"], m.get("poolpart", "")) for p in self.settings.data["pools"] for m in p["members"]}
            for m in members:
                if (m["uuid"], m.get("poolpart", "")) in used:
                    raise ValueError("Eine der Platten gehört bereits zu einem anderen Pool")
                _, part = self.disks.partition(m["uuid"])
                if not part:
                    raise ValueError(f"Partition {m['uuid']} nicht gefunden")
                if m.get("poolpart") == ".":
                    pass   # vorhandenen Inhalt direkt einbinden (z. B. Freigabe eines externen NAS)
                elif not m.get("poolpart"):
                    # Neue Platte: eigenen DrivePool-kompatiblen Ordner anlegen (nur mit Schreibzugriff)
                    if self.options["read_only"]:
                        raise ValueError("Für neue (leere) Platten muss 'read_only' in den "
                                         "Add-on-Optionen ausgeschaltet sein")
                    pp = f"PoolPart.{uuidlib.uuid4()}"
                    os.makedirs(os.path.join(config.DISK_MNT, m["uuid"], pp), exist_ok=True)
                    m["poolpart"] = pp
            pool = {
                "id": pid,
                "name": name,
                "members": [{"uuid": m["uuid"], "poolpart": m["poolpart"]} for m in members],
                "default_target": int(default_target),
                "targets": {},
                "adopted_drivepool": bool(adopt),
            }
            self.settings.data["pools"].append(pool)
            self.settings.save()
        self.mount(pool)
        self.start_scan(pool)
        return pool

    def delete(self, pid):
        pool = self.settings.pool(pid)
        if not pool:
            raise ValueError("Pool nicht gefunden")
        self.unmount(pool)
        with self.settings.lock:
            self.settings.data["pools"] = [p for p in self.settings.data["pools"] if p["id"] != pid]
            self.settings.save()
        self.index.delete_pool(pid)
        # Hinweis: Dateien auf den Platten bleiben unverändert erhalten.

    # ------------------------------------------------------------ Einhängen
    def mount(self, pool):
        for m in pool["members"]:
            _, part = self.disks.partition(m["uuid"])
            if part and not part["mountpoint"]:
                try:
                    self.disks.mount(m["uuid"])
                except Exception as err:  # noqa: BLE001
                    log.warning("Mitglied %s nicht einhängbar: %s", m["uuid"], err)
        self.disks.refresh()
        if self.is_mounted(pool):
            return
        present = [b for b in self.branches(pool) if b[2]]
        if not present:
            self.errors[pool["id"]] = "Keine Platte des Pools verfügbar"
            raise RuntimeError(self.errors[pool["id"]])
        mp = self.mountpoint(pool)
        os.makedirs(mp, exist_ok=True)
        ro = self.options["read_only"]
        opts = ["defaults", "allow_other", "cache.files=off", "dropcacheonclose=true",
                "category.create=mfs", "moveonenospc=true", "minfreespace=20G",
                f"fsname=raidpool-{pool['id']}"]
        if ro:
            opts.append("ro")
        branch_str = ":".join(b[1] + ("=RO" if ro else "=RW") for b in present)
        code, _, err = run(["mergerfs", "-o", ",".join(opts), branch_str, mp], timeout=60)
        if code != 0:
            self.errors[pool["id"]] = err.strip() or "mergerfs-Fehler"
            raise RuntimeError(self.errors[pool["id"]])
        self.errors.pop(pool["id"], None)
        log.info("Pool %s eingehängt unter %s (%d/%d Platten)", pool["id"], mp, len(present), len(pool["members"]))

    def unmount(self, pool):
        if config.DEV or not self.is_mounted(pool):
            return
        mp = self.mountpoint(pool)
        code, _, err = run(["fusermount3", "-u", mp])
        if code != 0:
            code, _, err = run(["umount", mp])
        if code != 0:
            raise RuntimeError(err.strip() or "Pool wird noch benutzt")

    def mount_all(self):
        for p in list(self.settings.data["pools"]):
            try:
                self.mount(p)
            except Exception as err:  # noqa: BLE001
                log.error("Pool %s: %s", p["id"], err)

    # ------------------------------------------------------------ Scan & Ziele
    def start_scan(self, pool):
        t = self.scan_threads.get(pool["id"])
        if t and t.is_alive():
            raise ValueError("Scan läuft bereits")
        branches = [(u, p) for u, p, ok in self.branches(pool) if ok]
        t = threading.Thread(target=self.index.scan, args=(pool, branches), daemon=True,
                             name=f"scan-{pool['id']}")
        self.scan_threads[pool["id"]] = t
        t.start()

    def set_target(self, pid, path, value):
        with self.settings.lock:
            pool = self.settings.pool(pid)
            if not pool:
                raise ValueError("Pool nicht gefunden")
            if value in (None, "", 0, "0"):
                pool["targets"].pop(path, None)
            else:
                v = int(value)
                if v < 1 or v > len(pool["members"]):
                    raise ValueError(f"Duplizierung muss zwischen 1 und {len(pool['members'])} liegen")
                pool["targets"][path] = v
            self.settings.save()
        self.index.aggregate(pool)

    def adopt_targets(self, pid):
        pool = self.settings.pool(pid)
        if not pool:
            raise ValueError("Pool nicht gefunden")
        sug = self.index.suggest_targets(pid)
        with self.settings.lock:
            pool["default_target"] = sug.pop("", pool.get("default_target", 1))
            pool["targets"] = sug
            self.settings.save()
        self.index.aggregate(pool)
        return pool

    # ------------------------------------------------------------ Status
    def status(self, pool):
        members = []
        total = free = used_disk = 0
        for uuid, path, ok in self.branches(pool):
            disk, part = self.disks.partition(uuid)
            u = part["usage"] if part else None
            if u:
                total += u["total"]
                free += u["free"]
                used_disk += u["used"]
            members.append({
                "uuid": uuid,
                "path": path,
                "available": ok,
                "disk": disk["path"] if disk else None,
                "model": disk["model"] if disk else None,
                "label": part["label"] if part else None,
                "usage": u,
                "smart_state": None,
            })
        root = self.index.folder(pool["id"], "") or {}
        stored = root.get("stored") or 0
        return {
            **pool,
            "mounted": self.is_mounted(pool),
            "mountpoint": self.mountpoint(pool),
            "read_only": self.options["read_only"],
            "error": self.errors.get(pool["id"]),
            "degraded": any(not m["available"] for m in members),
            "members": members,
            "space": {
                "total": total,
                "free": free,
                "used": used_disk,
                "pool_logical": root.get("bytes") or 0,     # Daten ohne Duplikate
                "pool_stored": stored,                      # inkl. Duplikate
                "duplication_overhead": stored - (root.get("bytes") or 0),
                "other": max(used_disk - stored, 0),        # Fremddaten außerhalb des Pools
            },
            "files": root.get("files") or 0,
            "dup": {k: root.get(k) or 0 for k in ("c1", "c2", "c3", "inconsistent", "under")},
            "scan": self.index.scan_info(pool["id"]),
        }

    def pool_usage_fallback(self, pool):
        return _usage(self.mountpoint(pool))
