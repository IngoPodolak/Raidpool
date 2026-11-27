"""Echtzeit-Erkennung über Samba: das VFS-Modul full_audit meldet jede geschlossene, umbenannte oder
gelöschte Datei in der Samba-Ausgabe (smb.py reicht diese Zeilen an feed() weiter). Das funktioniert
unabhängig von der Ordnerzahl und vom inotify-Kontingent."""
import collections
import logging
import os
import time
import queue
import threading

log = logging.getLogger("nas")
TAG = "RAIDPOOL|"


class AuditListener:
    def __init__(self, handler):
        self.handler = handler          # handler(op, abs_path, abs_new_or_None)
        self.active = False
        self.error = None
        self.events = 0
        self.recent = collections.deque(maxlen=30)   # zuletzt geänderte Dateien (für die Live-Ansicht)

    def start(self):
        # Verarbeitung in eigenem Thread: die Samba-Ausgabe darf nie warten müssen, sonst stockt Samba
        self.q = queue.SimpleQueue()
        threading.Thread(target=self._loop, daemon=True, name="smb-audit").start()
        self.active = True

    def feed(self, line):
        if self.active:
            self.q.put(line)

    def _loop(self):
        while True:
            line = self.q.get()
            try:
                self._parse(line)
            except Exception:  # noqa: BLE001
                log.debug("Audit-Meldung nicht verarbeitet", exc_info=True)

    def _parse(self, msg):
        i = msg.find(TAG)
        if i < 0:
            return
        parts = msg[i + len(TAG):].rstrip("\n\0").split("|")
        # connectpath | op | ok | arg1 [| arg2]
        if len(parts) < 4 or parts[2] != "ok":
            return
        root, op, args = parts[0], parts[1], parts[3:]

        def absolute(p):
            p = p.strip()
            if not p or p == ".":
                return None
            return os.path.normpath(p if p.startswith("/") else os.path.join(root, p))

        if op == "close":
            path = absolute("|".join(args))
            if path:
                self.events += 1
                self.handler("write", path, None)
        elif op == "unlinkat":
            path = absolute("|".join(args))
            if path:
                self.events += 1
                self.handler("delete", path, None)
        elif op == "renameat" and len(args) >= 2:
            # Bei '|' im Dateinamen ist die Trennung mehrdeutig -> Mitte als Trenner annehmen
            mid = len(args) // 2
            old, new = absolute("|".join(args[:mid])), absolute("|".join(args[mid:]))
            if old and new:
                self.events += 1
                self.handler("rename", old, new)
