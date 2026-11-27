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
    json.dump({"publish_sensors": False, "log_level": "debug"}, fh)
print("Test-DrivePool angelegt unter", ROOT)
