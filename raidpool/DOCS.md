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

## Netzwerkfreigaben

Unter **Platten → + Netzwerkfreigabe** lassen sich SMB- (Windows, Synology, QNAP, …) und
NFS-Freigaben einbinden. Sie verhalten sich danach wie eine Platte:

- **In einen Pool aufnehmen** – entweder mit eigenem `PoolPart`-Ordner (Pool-Daten getrennt vom
  restlichen Inhalt der Freigabe) oder **direkt** (der gesamte Inhalt erscheint im Pool, z. B. um
  ein externes NAS oder einen anderen Pool einzubinden).
- **Nach Duplikaten durchsuchen** – unter Duplikate → Einstellungen → „Zusätzlich durchsuchen“.

Mit `read_only: true` werden auch Freigaben nur lesend verbunden. Netzwerkfreigaben haben keine
SMART-Werte und keine Energieeinstellung.

## Duplikate

Reiter **Duplikate** findet Dateien mit identischem Inhalt – egal ob gleicher oder anderer Name.
Kopien, die der Pool selbst zur Duplizierung anlegt, werden nicht mitgezählt.

- **Vergleich:** gleiche Größe → Schnellvergleich (Anfang, Mitte, Ende) → kompletter Inhalt.
  Gleicher Name bei anderem Inhalt ist kein Duplikat. Vor dem Löschen wird jede Datei noch einmal
  Byte für Byte mit einer verbleibenden Kopie verglichen. Mindestens eine Datei pro Gruppe bleibt immer.
- **Ignorieren:** „einmal“ blendet bis zur nächsten Suche aus, „immer“ dauerhaft.
  Ganze Ordner lassen sich in den Einstellungen ausschließen.
- **Papierkorb:** Gelöschte Dateien landen zuerst in `.nas-trash` auf der jeweiligen Platte und
  werden nach der eingestellten Zeit endgültig gelöscht.
- **Ausführung:** manuell, nach Zeitplan oder automatisch im Leerlauf. Die Suche läuft mit
  niedrigster CPU- und I/O-Priorität, ist in der Geschwindigkeit begrenzt und verdrängt keine
  anderen Daten aus dem Cache. Im Automatik-Modus pausiert sie, sobald der Rechner beschäftigt ist.
- Löschen ist erst möglich, wenn `read_only` ausgeschaltet ist.

## Energie / Dauerbetrieb

Pro Platte einstellbar unter **Platten → Energie** (Standard über die Option `power_default`):

- **Immer an (Dauerbetrieb)** – Standard. Kein Standby, Kopfparken aus, alle 3 Minuten ein
  kurzer Lesezugriff direkt auf die Platte. Das hält auch USB-Gehäuse wach, die sich unabhängig
  vom Betriebssystem abschalten.
- **Herstellervorgabe** – nichts wird verändert.
- **Abschalten nach …** – Standby nach 30 min bis 5 h.

Zum Beobachten gibt es pro Platte die Zähler „Anläufe“ und „Kopf-Parkvorgänge“
(auch als Sensoren). Steigen die Parkvorgänge im Dauerbetrieb trotzdem schnell, unterstützt die
Platte das Abschalten des Kopfparkens nicht über APM (z. B. WD Green – dort hilft `idle3-tools`).

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
| `power_default` | Standard-Energiemodus: `always`, `vendor` oder `spindown` |
| `spindown_minutes` | Zeit bis zum Abschalten im Modus `spindown` |
| `smart_interval_minutes` | Abstand der SMART-Abfragen |
| `smart_wake_disks` | Schlafende Platten für SMART wecken |
| `publish_sensors` | Sensoren an Home Assistant senden |
| `sensor_interval_seconds` | Aktualisierung der Sensoren |

## Fahrplan

1. ✅ Pool, DrivePool-Übernahme, SMART, Duplizierungs-Analyse, Weboberfläche
2. Duplizierung herstellen/abbauen nach Soll, Ausbalancieren, Reparatur bei Plattenausfall
3. SMB-Freigaben und Benutzer (Namen aus HA, eigenes SMB-Passwort)
4. SSD-Cache (fest je Pool, geteilt oder System-SSD; Mover auf die Platten)
5. RAID-Modi (mdadm), eigene HA-Integration

✅ Duplikatsuche (0.3.0)
