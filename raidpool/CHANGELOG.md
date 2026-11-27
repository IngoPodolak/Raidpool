# Changelog

## 0.19.5
- **Schöneres HA-Dashboard:** Tortendiagramme in TB mit Anteilen in %, Pool-Diagramm in voller Breite;
  je Platte ein kleines Diagramm „xx % belegt“ (zwei nebeneinander) und darunter eine Zeile mit
  Belegung, Temperatur, freiem Platz und Schalter für die Direktfreigabe. Kurze, eindeutige Plattennamen
  (z. B. „WD100EFAX · 39D“).

## 0.19.4
- „Dashboard in HA erzeugen“ trägt apexcharts-card und Bubble Card als Dashboard-Ressource ein, falls sie
  installiert, aber nicht eingetragen sind (sonst zeigt HA bei den Tortendiagrammen „Konfigurationsfehler“).

## 0.19.3
- **Schnelleres Kopieren vieler Dateien:** Der Pool hält Dateiinfos, Ordnerlisten und „gibt es nicht“-Antworten
  2 Sekunden im Zwischenspeicher. Samba prüft beim Anlegen jeder Datei, ob es sie (auch in anderer
  Groß-/Kleinschreibung) schon gibt – vorher ging das jedes Mal durch alle Platten und beide Pool-Ebenen.
  Kennt die installierte mergerfs-Version eine Option nicht, wird ohne eingehängt.

## 0.19.2
- Live-Ansicht zeigt die CPU-Last je Programm im Add-on (Samba, mergerfs, NTFS-Treiber, RAIDPool) –
  hilft zu sehen, was einen Kopiervorgang bremst.
- Der Abgleich nach dem Start läuft erst nach 15 Minuten (nicht mehr direkt nach jedem Update).

## 0.19.1
- Während der Mover den SSD-Cache auf die Platten verteilt (inkl. aller Kopien), ruht die
  Platte-zu-Platte-Duplizierung; Scans, Abgleich, Ausbalancieren und Duplikatsuche pausieren ebenfalls.
  Wartende Dateien werden danach bearbeitet. Die Live-Ansicht zeigt das an.

## 0.19.0
- **Downloads auf der SSD fertigstellen** (SSD-Cache, standardmäßig an): Archiv- und Downloadteile (.rar,
  .part, .7z, .zip, .001 …) bleiben auf der SSD (bis 24 h einstellbar) – sie werden nach dem Entpacken
  meist gelöscht und müssen so nie auf die Platten. Ein Ordner wird erst verschoben, wenn darin die
  eingestellte Zeit nichts mehr passiert ist (Download bzw. Entpacken fertig). Wird die SSD voll, wird
  trotzdem verschoben.

## 0.18.0
- **Vorrang für Kopiervorgänge:** Wird über die Freigaben viel geschrieben (Datei zum Schreiben geöffnet und
  mehr als 20 MB/s), pausieren Scans, nächtlicher Abgleich, Ausbalancieren und Duplikatsuche. Neue Dateien
  werden weiter sofort bearbeitet. Die Live-Ansicht zeigt „Kopiervorgang erkannt“.
- **SSD-Cache → Platten parallel:** Der Mover schreibt mehrere Dateien gleichzeitig auf verschiedene
  Platten (eine Datei je Platte) und legt dabei direkt die Soll-Anzahl Kopien an – einmal von der SSD
  lesen, auf zwei Platten gleichzeitig schreiben. Kein zusätzlicher Duplizierungsschritt mehr nötig.

## 0.17.0
- **Live-Ansicht** oben in „Übersicht“ und „Duplizierung“ (alle 2 Sekunden aktualisiert, zuklappbar):
  laufende Vorgänge mit Fortschritt und aktueller Datei (Duplizieren inkl. Geschwindigkeit, SSD-Cache →
  Platten, Scans, Abgleich, Duplikatsuche, Dateisystem-Prüfung, Warteschlange), Auslastung von CPU,
  Arbeitsspeicher und RAIDPool selbst, Lese-/Schreibrate und Auslastung jeder Platte, über die Freigaben
  gerade geöffnete Dateien (liest/schreibt) und zuletzt geänderte Dateien.

## 0.16.0
- **Duplikatsuche viel schneller und schonender:** Statt Kandidaten komplett zu lesen, vergleicht sie
  standardmäßig Größe und Inhalt an 10 Stellen (ca. 0,8 MB je Datei; Dateien bis 1 MB ganz). Bei großen
  Videos z. B. 0,8 MB statt mehrerer GB. Vor dem Löschen wird wie bisher jede Datei Byte für Byte mit der
  bleibenden verglichen – ein Fehltreffer kann nichts löschen.
- Einstellung „Vergleich“: Schnell (Stichproben, Standard) oder Gründlich (komplett lesen).
- Stichproben werden gespeichert und bei der nächsten Suche wiederverwendet.

## 0.15.2
- „Abbrechen“ der Duplikatsuche wirkte im Schnell- und Vollvergleich nicht (wurde als Lesefehler der
  aktuellen Datei gewertet, die Suche lief mit der nächsten weiter).

## 0.15.1
- **Duplizierung blieb stehen:** Eine kurz gesperrte Datenbank (während der Duplikatsuche) beendete den
  Duplizierungsdienst, die Warteschlange wuchs nur noch. Fehler werden jetzt abgefangen und später
  erneut versucht; die Duplikatsuche hält die Datenbank nicht mehr während langer Lesevorgänge gesperrt.
- Samba schreibt seine Meldungen in eine Datei statt in eine Pipe – kann Kopiervorgänge nicht mehr
  ausbremsen, wenn RAIDPool kurz beschäftigt ist.
- Dateien, die noch auf dem SSD-Cache liegen, landen nicht mehr unnötig in der Warteschlange (der
  Mover meldet sie nach dem Verschieben).
- Ordnerstatistik bei großen Pools höchstens alle 5 Minuten neu berechnen (spart CPU).

## 0.15.0
- **Duplikatsuche in bestimmten Ordnern:** „Ordner durchsuchen …“ wählt einen oder mehrere Ordner (auch
  ganze Pools oder Netzwerkquellen). Wahlweise mit allen anderen Dateien vergleichen (findet auch Kopien
  außerhalb) oder nur innerhalb der gewählten Ordner. Die letzte Suche zeigt, worauf sie beschränkt war.

## 0.14.0
- **Schneller Scan:** Abgleich und Duplikatsuche lesen nur noch Ordner neu ein, deren Änderungszeit sich
  geändert hat (neue, gelöschte, umbenannte Einträge) – meist Sekunden statt vieler Minuten, kaum
  Plattenlast. Unveränderte Ordner werden nicht einmal aufgelistet.
- Vollständiger Scan nur noch einmal pro Woche, beim ersten Mal, nach Platten-Änderungen (z. B. Ausfall)
  oder auf Knopfdruck („Neu scannen“).

## 0.13.3
- **Dateiliste bleibt bei Abbruch vollständig:** Ein Scan baut die neue Liste erst im Hintergrund auf und
  tauscht sie am Ende in einem Schritt aus. Vorher war die Liste nach einem Neustart/Update mitten im Scan
  nur teilweise gefüllt (Ordner zeigten plötzlich nur noch wenige Dateien – die Dateien selbst waren da).
- Wurde ein Scan abgebrochen, startet er nach dem Neustart automatisch neu.

## 0.13.2
- Echtzeit über Samba funktionierte nicht (/dev/log ist im Container schreibgeschützt). Die Meldungen
  kommen jetzt direkt aus der Samba-Ausgabe; im Log erscheinen sie nicht.
- Duplikatsuche erstellt die Pool-Dateiliste nur neu, wenn sie älter als 6 Stunden ist, und zeigt dabei
  den Fortschritt. Ein gleichzeitiger zweiter Scan desselben Pools wird verhindert (zählte Kopien doppelt).

## 0.13.1
- **Keine Fehlalarme „Platte ausgefallen“ mehr bei Last:** Antwortet eine Platte nur langsam (z. B. Scan
  und Downloads gleichzeitig), ist das Gerät aber vorhanden, wird sie bis zu 5 Minuten beobachtet statt
  sofort aus dem Pool genommen. Echte Ausfälle (Platte weg) werden weiter sofort erkannt.

## 0.13.0
- **Echtzeit-Duplizierung über Samba:** Jede über die Freigaben geschriebene, umbenannte oder gelöschte
  Datei meldet Samba direkt an RAIDPool – unabhängig von der Ordnerzahl (der HAOS-Kernel hat kein
  fanotify, das inotify-Kontingent reicht bei großen Pools nicht). Reines Lesen/Abspielen löst nichts aus.
- Vom SSD-Cache auf die Platten verschobene Dateien werden sofort dupliziert.
- **Netzwerkspeicher in HA, die auf RAIDPool zeigen** (z. B. Backup-Ziel \\SERVER\Backup), werden
  automatisch neu geladen, sobald RAIDPool läuft – beim HA-Start sind sie sonst „fehlgeschlagen“.

## 0.12.4
- Weboberfläche bleibt während eines laufenden Scans/Abgleichs bedienbar (zeigt kurz die letzten Werte),
  statt bis zum Ende zu warten und nicht zu laden.

## 0.12.3
- **Echtzeit-Duplizierung für große Pools:** neue und geänderte Dateien werden jetzt per fanotify für die
  ganze Platte erkannt – unabhängig von der Anzahl der Ordner. Vorher stand bei großen Pools
  „auf 0 Ordner begrenzt“ und dupliziert wurde erst beim Abgleich.
- Ordner-Überwachung (für Löschen/Verschieben) nutzt bis zu einem Drittel des Systemkontingents.

## 0.12.2
- **Schreibschutz erkannt:** Hängt der NTFS-Treiber eine Platte stillschweigend nur lesend ein (Windows-
  Schnellstart/Ruhezustand oder unsauberes Dateisystem), zeigt RAIDPool das jetzt an („unsauber“ +
  „nur lesend“) statt fälschlich [rw]. Vorher war dann der ganze Pool schreibgeschützt.
- „Prüfen und reparieren“ verwirft bei NTFS einen vorhandenen Windows-Ruhezustand und stellt so den
  Schreibzugriff wieder her.

## 0.12.1
- **Wichtig:** Die Echtzeit-Überwachung belegt nicht mehr das gesamte inotify-Kontingent des Systems.
  Vorher konnten dadurch u. a. Netzwerkspeicher (Backup-Ziel) in Home Assistant nicht eingehängt werden
  („No space left on device“). Jetzt höchstens ein Drittel, Rest per regelmäßigem Abgleich.
- Samba: Log-Meldungen „idmap range not specified“ und „usershares … Permission denied“ abgestellt.

## 0.12.0
- **SSD-Cache pro Pool**: neue Dateien landen zuerst auf der SSD und werden vom Mover auf die Platten
  verschoben (nach Alter, ab Füllstand und täglich zur eingestellten Uhrzeit), dort dupliziert.
- Cache-Ziel: **System-SSD von Home Assistant** (Ordner im Datenbereich, nicht in HA-Backups) oder eine
  **eigene SSD**; eine SSD kann von mehreren Pools geteilt werden. Größenlimit und Mindest-Freiplatz.
- Dateien in Benutzung werden nicht verschoben; Pause bei USV-Batteriebetrieb
- Beim Ausschalten/Wechsel wird der Cache vorher geleert; MQTT-Sensoren und Taste „Cache verschieben“
- Datei-Index wird nicht mehr in HA-Backups gesichert (wird neu aufgebaut)

## 0.11.0
- Neuer Reiter **RAID**: Software-RAID (mdadm) anlegen – RAID 0, 1, 5, 6, 10 und JBOD – mit ext4 oder XFS.
  Nur leere bzw. freie Platten wählbar (keine Pool-, System- oder DrivePool-Platten), Bestätigung durch
  Eintippen des Namens. Anzeige des nutzbaren Speichers schon bei der Auswahl.
- Status mit Erstsynchronisation/Wiederaufbau in %, Platten-Zustand, Prüfung, Platte ersetzen, Löschen
- Verbünde werden beim Start automatisch zusammengesetzt und eingehängt, erscheinen unter „Platten“
  und können in Pools aufgenommen werden
- Meldung in HA, wenn ein Verbund beeinträchtigt ist; MQTT-Gerät je Verbund (Zustand, Fortschritt, Problem, Prüfen)

## 0.10.1
- Erkennt den **Schutzmodus** der App: Hinweis in der Oberfläche und im Protokoll, statt der
  irreführenden Meldung zum Windows-Schnellstart
- Klare Fehlermeldung, wenn der Zugriff auf eine Platte verweigert wird

## 0.10.0
- Name überall nur noch **RAIDPool** (auch MQTT-Gerät, Seitenleiste, Meldungen)
- Neue Entitäten: **Größe, Frei, Belegt (GB)** je Platte; je Pool zusätzlich Belegt (GB),
  Duplizierte Kopien und Sonstiges – passend für Tortendiagramme
- Weboberfläche: **Tortendiagramme** für Pools (Daten/Duplikate/Sonstiges/Frei) und Platten (Belegt/Frei)
- **Dashboard „RAIDPool“ in HA erzeugen** (Knopf in der Übersicht): Tortendiagramme mit apexcharts-card
  und Bubble Cards (Füllstand als Slider, Schalter, Tasten) für alle Pools und Platten

## 0.9.0
- **Integration in Home Assistant über MQTT-Discovery** – kommt automatisch mit dem Add-on, sobald
  der Mosquitto-Broker läuft. Geräte für Add-on, jeden Pool und jede Platte mit Sensoren,
  Problem-Sensoren, Schaltern (SMB, Duplizierung, Direktfreigaben) und Tasten (Abgleich,
  Ausbalancieren, Scan, Duplikatsuche, SMART). Entfernte Platten verschwinden auch aus HA.
  Ohne Broker werden wie bisher einfache Sensoren gesetzt.
- **Direktfreigaben**: jede Platte eines Pools zusätzlich als eigene Freigabe (`\\server\Platte-<Name>`),
  Haupt- und Einzelschalter in der Oberfläche und als Schalter in HA, eigene Zugriffsrechte.

## 0.8.0
- **Weitere Servernamen**: HA ist zusätzlich z. B. als `\\SERVER` erreichbar (NetBIOS-Alias),
  mit Prüfung, ob der Name im Netzwerk noch belegt ist.
- **Zusätzliche IP-Adressen**: die alte IP des Windows-Servers übernehmen – mit Konfliktprüfung (ARP),
  automatischem Wiederherstellen und Entfernen beim Stoppen.
- **Weitere Anmeldenamen** pro Benutzer (z. B. die E-Mail-Adresse des Microsoft-Kontos), damit
  gespeicherte Anmeldungen der Geräte weiter funktionieren.
- **Freigaben vom alten Server übernehmen**: Namen, Beschreibung und (mit Adminrechten) Ordner werden
  ausgelesen und dem Pool zugeordnet. Zugangsdaten werden nicht gespeichert.

## 0.7.0
- **Plattenausfall im Betrieb** wird erkannt: Platte wird aus dem Pool gelöst (ohne Aushängen des
  Pools), Meldung in Home Assistant, fehlende Kopien werden sofort auf die übrigen Platten kopiert.
  Kommt die Platte zurück (z. B. USB), wird sie automatisch wieder eingebunden.
- **Platte hinzufügen** und **Platte ersetzen** für bestehende Pools – danach automatischer Abgleich.
- **Dateisystem-Prüfung (wie chkdsk)**: Prüfen bzw. Prüfen und reparieren pro Partition – NTFS mit
  `ntfsck` (Fallback `ntfsfix`), ext4, XFS, exFAT, btrfs (nur prüfen). Live-Protokoll, die Platte wird
  dafür kurz aus dem Pool gelöst.
- **SMART-Oberflächentest** (wie chkdsk /r) pro Platte.
- **Unsaubere NTFS-Platten** (z. B. nach Stromausfall) werden erkannt: nur lesend einbinden und melden
  (Standard), automatisch reparieren oder trotzdem einbinden.
- **USV über NUT**: bei Batteriebetrieb Pause von Duplizierung/Duplikatsuche/Ausbalancieren, optional
  Aushängen bei niedrigem Akku.
- Reste abgebrochener Kopien (`.nas-tmp-*`) werden aufgeräumt und nie mitgezählt.
- Basis jetzt Debian trixie (neuere Samba/mergerfs, ntfsck verfügbar).

## 0.6.0
- **Duplizierung wird hergestellt**: fehlende Kopien werden auf die Platte mit dem meisten freien
  Platz kopiert (Temp-Datei → Größenprüfung → umbenennen), Änderungszeit bleibt erhalten.
- **Echtzeit**: neue und geänderte Dateien werden nach kurzer Ruhezeit dupliziert, Änderungen an
  einer Kopie auf die anderen Platten übertragen, Umbenennen/Löschen über den Pool erkannt.
- **Täglicher Abgleich** (Standard 04:00) und beim Start – fängt alles ab, was die Echtzeit verpasst.
- **Unterschiedliche Kopien**: neueste übernehmen oder nur melden; gleich alte Kopien mit anderem
  Inhalt werden nie automatisch entschieden. Dialog „Lösen …“ zum Auswählen der richtigen Version.
- **Ausbalancieren** (manuell oder täglich) ab einstellbarem Füllstands-Unterschied, mit Obergrenze pro Lauf.
- **Platte aus Pool nehmen**: Dateien werden vorher umkopiert; auf der Platte selbst wird nichts gelöscht.
- Sicherheit: DrivePool-Übernahme setzt das Soll automatisch auf die erkannte Duplizierung;
  „Überzählige Kopien entfernen“ ist standardmäßig aus. Alles nur mit ausgeschaltetem `read_only`.

## 0.5.0
- Neuer Reiter **Freigaben** (SMB/Samba): ganze Pools und einzelne Ordner freigeben – auch beides
  gleichzeitig. Ordnerauswahl per Popup, fehlende Ordner werden angelegt.
- Rechte pro Freigabe und Benutzer (kein Zugriff / lesen / lesen + schreiben) und optionaler Gastzugriff
- Benutzer aus Home Assistant übernehmen (eigenes SMB-Passwort) oder reine SMB-Benutzer anlegen,
  Benutzer sperren, Passwort ändern
- Optionen: im Netzwerk sichtbar, Papierkorb (`.nas-recycle`), macOS-Optimierung, Time Machine mit Größenlimit
- Anzeige in der Windows-Netzwerkumgebung (WS-Discovery), aktive Verbindungen, Server-Einstellungen
- Das Add-on nutzt jetzt das Host-Netzwerk (für Port 445). Läuft das offizielle Samba-Add-on, wird das erkannt.

## 0.4.0
- **Netzwerkfreigaben** (SMB/CIFS und NFS) einbinden – Reiter Platten → „+ Netzwerkfreigabe“.
  Passwörter werden getrennt in `/data` gespeichert (nicht im sichtbaren Konfigurationsordner).
- Freigaben (und Platten) als **Pool-Mitglied**: mit eigenem PoolPart-Ordner oder
  „vorhandenen Inhalt direkt einbinden“ (z. B. ein externes NAS oder einen anderen Pool).
  Direkt einbinden geht auch mit aktivem Schreibschutz.
- **Duplikatsuche über zusätzliche Quellen**: Platten oder Freigaben außerhalb der Pools
  können mit durchsucht werden – Duplikate werden auch zwischen Pool und Quelle gefunden.

## 0.3.1
- Ordner ausschließen per Ordnerauswahl (Popup) statt Texteingabe; Ausschlüsse wirken sofort
- Liste „Dauerhaft ignoriert“ mit „Wieder anzeigen“ (einzeln oder alle)
- Korrektur: leere Auswahlleiste wurde manchmal angezeigt

## 0.3.0
- Neuer Reiter **Duplikate**: findet inhaltsgleiche Dateien (z. B. doppelt heruntergeladen),
  auch mit unterschiedlichen Namen. Pool-Duplizierung zählt nicht als Duplikat.
- Vergleich in Stufen: Größe → Anfang/Mitte/Ende → kompletter Inhalt; vor dem Löschen zusätzlich
  Byte-für-Byte. Hash-Cache, erneute Suchen lesen nur neue/geänderte Dateien.
- Auswahl zum Löschen mit Pfadvergleich, automatisch markieren (neueste/älteste/kürzester Pfad),
  einmalig oder dauerhaft ignorieren, Ordner ausschließen, Papierkorb mit automatischer Leerung.
- Ausführung manuell, nach Zeitplan (Tage + Uhrzeit) oder automatisch im Leerlauf
  (pausiert bei CPU-/Plattenlast). Niedrigste CPU- und I/O-Priorität, Lesegeschwindigkeit begrenzbar.

## 0.2.0
- Energieverwaltung pro Platte: Dauerbetrieb (Standard), Herstellervorgabe oder Abschalten nach Zeit
- Dauerbetrieb: Standby-Timer aus (hdparm -S 0), Kopfparken aus (APM 254), zusätzlich alle 3 min
  ein kurzer Lesezugriff – hält auch USB-Gehäuse wach, die sich selbst abschalten
- SMART: Anläufe (Motor) und Kopf-Parkvorgänge, Warnung ab 300.000 Parkvorgängen, eigene Sensoren

## 0.1.0
- Erste Version: Platten- und SMART-Übersicht, DrivePool-Übernahme (nur lesend), Pool per
  mergerfs, Duplizierungs-Analyse pro Ordner, Soll-Duplizierung, Sensoren, Weboberfläche.
