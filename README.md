# RAIDPool

NAS-Add-on für Home Assistant: Festplatten-Pool wie StableBit DrivePool mit Duplizierung pro
Ordner, Übernahme bestehender DrivePool-Platten (NTFS), SMART-Überwachung, Sensoren und
Weboberfläche direkt in Home Assistant.

Geplant: SMB-Freigaben mit Benutzerverwaltung, SSD-Cache, RAID-Modi, Duplikatsuche.

## Installation

1. Home Assistant → Einstellungen → Add-ons → Add-on Store → ⋮ → Repositories
2. `https://github.com/IngoPodolak/raidpool` hinzufügen
3. „RAIDPool“ installieren und starten

Unterstützt: amd64 und aarch64 (ARM).

Ausführliche Beschreibung: [raidpool/DOCS.md](raidpool/DOCS.md)

## Entwicklung ohne echte Platten

```bash
python3 raidpool/tests/make_fake_drivepool.py /tmp/nasdev
cd raidpool/rootfs/opt/nas && NAS_DEV=1 NAS_PORT=8199 python3 -m nas
# Browser: http://127.0.0.1:8199
```
