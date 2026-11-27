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

## Duplizierung herstellen

Der **Duplizierungsdienst** (Reiter Duplizierung, oben) sorgt dafür, dass jede Datei so oft auf
verschiedenen Platten liegt, wie es das Soll ihres Ordners vorgibt.

- **Echtzeit:** neue oder geänderte Dateien werden nach einer kurzen Ruhezeit (Standard 30 s)
  dupliziert. Wird eine Kopie geändert, wird die Änderung auf die anderen Platten übertragen.
- **Täglicher Abgleich** (Standard 04:00) und beim Start des Add-ons.
- **Neue Kopien** entstehen auf der Platte mit dem meisten freien Platz, zuerst als Temp-Datei,
  werden geprüft und erst dann umbenannt. Pro Platte bleibt ein einstellbarer Rest frei.
- **Unterschiedliche Kopien:** je nach Einstellung gewinnt die neueste, oder es wird nur gemeldet.
  Sind beide gleich alt, wird nie automatisch entschieden – „Lösen …“ in der Dateiliste.
- **Überzählige Kopien entfernen** ist standardmäßig aus.
- **Ausbalancieren:** verschiebt Dateien von vollen auf leere Platten (manuell oder täglich).
- **Platte aus Pool nehmen** (Übersicht → Platte): kopiert vorher alles, was sonst fehlen würde.
- Bei der Übernahme eines DrivePools wird das Soll automatisch auf die erkannte Duplizierung gesetzt.
- Läuft nur, wenn `read_only` ausgeschaltet ist.

## Ausfall, Neustart, Stromausfall

- **Platte fällt aus:** wird nach ca. 20 s erkannt, aus dem Pool gelöst, in HA gemeldet; Dateien mit
  Duplizierung werden sofort auf die übrigen Platten nachkopiert. Dateien, die nur 1× auf der
  defekten Platte lagen, sind verloren (wie bei DrivePool). Übersicht → „Ersetzen …“ nimmt eine neue
  Platte auf und stellt das Soll wieder her.
- **Neustart (Updates):** Core-Updates betreffen das Add-on nicht. Bei OS-/Supervisor-Neustart wird
  sauber ausgehängt; nach dem Start läuft ein Abgleich, Reste abgebrochener Kopien werden entfernt.
- **Stromausfall:** Unsaubere NTFS-Platten werden erkannt und standardmäßig nur lesend eingebunden,
  bis sie geprüft sind (Platten → „Prüfen und reparieren“). Empfehlung: USV + NUT-Integration,
  dann pausiert das Add-on bei Batteriebetrieb und kann bei niedrigem Akku alles sauber aushängen.

## Dateisystem prüfen (chkdsk)

Platten → Partition → **Prüfen** bzw. **Prüfen und reparieren**. Für NTFS wird `ntfsck` verwendet
(echte Prüfung/Reparatur unter Linux, kann aber kein Windows-Journal nachspielen – Platten, die
zuletzt unsauber an Windows hingen, besser unter Windows prüfen). Ist `ntfsck` nicht verfügbar, wird
`ntfsfix` genutzt (nur grundlegende Reparaturen). „Oberflächentest“ startet den SMART-Selbsttest der Platte.

## Netzwerkfreigaben

Unter **Platten → + Netzwerkfreigabe** lassen sich SMB- (Windows, Synology, QNAP, …) und
NFS-Freigaben einbinden. Sie verhalten sich danach wie eine Platte:

- **In einen Pool aufnehmen** – entweder mit eigenem `PoolPart`-Ordner (Pool-Daten getrennt vom
  restlichen Inhalt der Freigabe) oder **direkt** (der gesamte Inhalt erscheint im Pool, z. B. um
  ein externes NAS oder einen anderen Pool einzubinden).
- **Nach Duplikaten durchsuchen** – unter Duplikate → Einstellungen → „Zusätzlich durchsuchen“.

Mit `read_only: true` werden auch Freigaben nur lesend verbunden. Netzwerkfreigaben haben keine
SMART-Werte und keine Energieeinstellung.

## Freigaben (SMB)

Reiter **Freigaben**: den **ganzen Pool** und/oder **einzelne Ordner** daraus als Netzwerkfreigabe
anbieten, z. B. `\\homeassistant\DrivePool` und zusätzlich `\\homeassistant\Filme`.

- **Rechte** pro Freigabe und Benutzer: kein Zugriff, lesen oder lesen + schreiben; optional Gastzugriff.
- **Benutzer**: aus Home Assistant übernehmen oder reine SMB-Benutzer (z. B. Fernseher) anlegen.
  Home Assistant gibt Passwörter nicht heraus, daher wird für SMB ein eigenes Passwort vergeben.
- **Papierkorb**: über die Freigabe gelöschte Dateien landen in `.nas-recycle/<benutzer>`.
- **macOS / Time Machine**: Optimierung für Mac und Time-Machine-Ziel mit Größenlimit.
- **Wichtig:** Das offizielle Samba-Add-on belegt ebenfalls Port 445 – beide gleichzeitig geht nicht.
  Ist es aktiv, zeigt der Reiter einen Hinweis; dort stoppen und hier „Starten“.
- Mit `read_only: true` sind alle Freigaben nur lesbar.

### Umzug vom Windows-Server (gleiche Adressen behalten)

1. Windows-Server/VM **ausschalten** (Name und IP dürfen nicht doppelt im Netz sein).
2. Freigaben → **Vom alten Server übernehmen …** (vorher, solange er noch läuft) – oder Freigaben
   gleich benennen wie bisher.
3. Server-Einstellungen → **Weitere Servernamen**: alten Namen eintragen, z. B. `SERVER`.
4. Falls Geräte die IP nutzen: **Zusätzliche IP-Adresse** = alte IP des Servers; die Adresse im
   Router für DHCP sperren.
5. Benutzer mit **gleichem Passwort** anlegen und unter „Anmeldenamen“ den bisherigen Login
   eintragen (z. B. die E-Mail-Adresse des Microsoft-Kontos).

Am zuverlässigsten ist zusätzlich ein DNS-Eintrag im Router (`SERVER` → IP von Home Assistant).

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
2. ✅ Duplizierung herstellen, Echtzeit, Ausbalancieren, Platte entfernen (0.6.0)
3. ✅ SMB-Freigaben und Benutzer (0.5.0)
4. SSD-Cache (fest je Pool, geteilt oder System-SSD; Mover auf die Platten)
5. RAID-Modi (mdadm), eigene HA-Integration

✅ Ausfallsicherheit, Dateisystem-Prüfung, USV (0.7.0)

✅ Duplikatsuche (0.3.0)
