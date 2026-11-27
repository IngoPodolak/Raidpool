# Changelog

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
