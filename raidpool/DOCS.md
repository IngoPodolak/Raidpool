# HA RAIDPool

Festplatten-Pool für Home Assistant nach dem Vorbild von StableBit DrivePool.

## Was Version 0.1 kann

- Platten erkennen (SATA/USB/NVMe) inkl. Modell, Seriennummer, Größe
- **SMART-Überwachung** (Temperatur, Betriebsstunden, kritische Attribute, NVMe-Verschleiß).
  Schlafende Platten werden standardmäßig **nicht** geweckt.
- **Bestehenden DrivePool übernehmen**: NTFS-Platten werden eingehängt, die Ordner
  `PoolPart.<GUID>` erkannt und per mergerfs zu einem Laufwerk zusammengefasst.
- **Duplizierung erkennen**: für jede Datei wird gezählt, auf wie vielen Platten sie liegt.
  Pro Ordner zeigt die Oberfläche Ist (z. B. 2× oder „gemischt 1–3×“), Soll, Dateien unter
  Soll und **abweichende Kopien** (gleiche Datei, unterschiedliche Größe).
- **Soll-Duplizierung pro Ordner** festlegen (vererbt sich an Unterordner) oder die erkannte
  DrivePool-Duplizierung mit einem Klick als Soll übernehmen.
- Sensoren in Home Assistant (`sensor.raidpool_<pool>_*`, `binary_sensor.raidpool_<pool>_problem`,
  `sensor.raidpool_disk_*`).

Die Dateien bleiben ganz normale Dateien auf NTFS – jede Platte ist an einem Windows-PC
weiterhin lesbar, und DrivePool kann die Platten jederzeit wieder übernehmen.

## Sicherheit

`read_only: true` (Standard) bindet **alles nur lesend** ein. Auf den Platten wird dann nichts
verändert. Erst wenn alles passt, `read_only` ausschalten.

Pool entfernen löscht **keine Dateien** – nur die Einstellung und den Index.

## Proxmox

Für SMART-Werte den **SATA-/HBA-Controller per PCIe durchreichen**. Werden nur einzelne
Platten durchgereicht (`qm set … -scsiX /dev/disk/by-id/…`), sieht HA nur „QEMU HARDDISK“
ohne SMART. Pool und Duplizierung funktionieren trotzdem.

Windows-Schnellstart: Waren die Platten zuletzt an einem Windows-PC mit aktivem Schnellstart,
lassen sie sich nur lesend einbinden. Windows vorher vollständig herunterfahren.

## Optionen

| Option | Bedeutung |
|---|---|
| `read_only` | Alles nur lesend einbinden (empfohlen für den Anfang) |
| `auto_mount_ntfs` | NTFS-Platten beim Start einhängen, um DrivePool-Ordner zu finden |
| `smart_interval_minutes` | Abstand der SMART-Abfragen |
| `smart_wake_disks` | Schlafende Platten für SMART wecken |
| `publish_sensors` | Sensoren an Home Assistant senden |
| `sensor_interval_seconds` | Aktualisierung der Sensoren |

## Fahrplan

1. ✅ Pool, DrivePool-Übernahme, SMART, Duplizierungs-Analyse, Weboberfläche
2. Duplizierung herstellen/abbauen nach Soll, Ausbalancieren, Reparatur bei Plattenausfall
3. SMB-Freigaben und Benutzer (Namen aus HA, eigenes SMB-Passwort)
4. SSD-Cache (fest je Pool, geteilt oder System-SSD; Mover auf die Platten)
5. RAID-Modi (mdadm), Duplikatsuche/-entfernung, eigene HA-Integration
