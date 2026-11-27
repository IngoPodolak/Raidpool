"""Erzeugt einen simulierten DrivePool für Tests ohne echte Platten (NAS_DEV=1)."""
import json
import os
import shutil
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/nasdev"
shutil.rmtree(ROOT, ignore_errors=True)

disks = {"A": "PoolPart.1111-aaaa", "B": "PoolPart.2222-bbbb", "C": "PoolPart.3333-cccc"}
for uuid, pp in disks.items():
    os.makedirs(f"{ROOT}/mnt/nas/disks/{uuid}/{pp}", exist_ok=True)
    os.makedirs(f"{ROOT}/mnt/nas/disks/{uuid}/Fremd", exist_ok=True)
    os.makedirs(f"{ROOT}/mnt/nas/disks/{uuid}/{pp}/.covefs", exist_ok=True)


def put(rel, size, on):
    for uuid in on:
        p = f"{ROOT}/mnt/nas/disks/{uuid}/{disks[uuid]}/{rel}"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"x" * (size if not isinstance(size, dict) else size[uuid]))


for i in range(5):
    put(f"Filme/Film {i}.mkv", 3000 + i, ["ABC"[i % 3]])          # 1×
for i in range(4):
    put(f"Fotos/2024/IMG_{i}.jpg", 500, ["A", "B"])               # 2×
    put(f"Fotos/2025/IMG_{i}.jpg", 500, ["B", "C"])               # 2×
put("Fotos/2025/IMG_kaputt.jpg", {"B": 500, "C": 499}, ["B", "C"])  # abweichend
for i in range(3):
    put(f"Dokumente/Steuer/doc{i}.pdf", 100, ["A", "B", "C"])     # 3×
put("Musik/a.mp3", 200, ["A"])

# Echte Duplikate (doppelt heruntergeladen) – inkl. Pool-Duplizierung, die NICHT zählen darf
import random
random.seed(7)


def blob(n):
    return random.randbytes(n)


def put_bytes(rel, data, on):
    for uuid in on:
        p = f"{ROOT}/mnt/nas/disks/{uuid}/{disks[uuid]}/{rel}"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)


film = blob(2_500_000)
put_bytes("Filme/Der Film (2024).mkv", film, ["A", "B"])          # im Pool 2× dupliziert
put_bytes("Downloads/Der Film (2024).mkv", film, ["C"])           # doppelt heruntergeladen
put_bytes("Downloads/alt/Der Film (2024).mkv", film, ["A"])       # noch einmal
foto = blob(400_000)
put_bytes("Fotos/2024/Urlaub_001.jpg", foto, ["A", "B"])
put_bytes("Handy-Backup/DCIM/IMG_20240712_101500.jpg", foto, ["C"])   # anderer Name, gleicher Inhalt
setup = blob(800_000)
put_bytes("Software/Treiber/setup.exe", setup, ["B"])
put_bytes("Software/Tool/setup.exe", setup, ["C"])                    # gehört zu versch. Programmen
a, b = blob(300_000), blob(300_000)
put_bytes("Dokumente/Vertrag.pdf", a, ["A"])
put_bytes("Downloads/Vertrag.pdf", b, ["B"])                          # gleicher Name+Größe, anderer Inhalt
c = bytearray(blob(500_000)); d = bytearray(c); d[250_000] ^= 1
put_bytes("Musik/Live.flac", bytes(c), ["A"])
put_bytes("Downloads/Live.flac", bytes(d), ["C"])                     # nur Mitte unterscheidet sich
put("Musik/b.mp3", 200, ["A", "C"])
put("Musik/Alben/c.mp3", 200, ["B"])

fake = {"blockdevices": [
    {"name": "sda", "path": "/dev/sda", "size": 64e9, "type": "disk", "model": "QEMU HARDDISK", "rota": "0",
     "children": [{"name": "sda8", "path": "/dev/sda8", "type": "part", "fstype": "ext4",
                   "label": "hassos-data", "uuid": "sys", "size": 60e9}]},
] + [
    {"name": f"sd{c}", "path": f"/dev/sd{c}", "size": s, "type": "disk", "model": m, "serial": f"SN{c.upper()}",
     "tran": "sata", "rota": "1",
     "children": [{"name": f"sd{c}2", "path": f"/dev/sd{c}2", "type": "part", "fstype": "ntfs",
                   "label": f"Daten{u}", "uuid": u, "size": s}]}
    for c, u, s, m in (("b", "A", 4e12, "WDC WD40EFRX"), ("c", "B", 8e12, "ST8000VN004"), ("d", "C", 12e12, "WDC WD120EFBX"))
]}
os.makedirs(f"{ROOT}/data", exist_ok=True)
with open(f"{ROOT}/fakedisks.json", "w") as fh:
    json.dump(fake, fh)
with open(f"{ROOT}/data/options.json", "w") as fh:
    json.dump({"publish_sensors": False, "log_level": "debug", "read_only": False}, fh)
print("Test-DrivePool angelegt unter", ROOT)
