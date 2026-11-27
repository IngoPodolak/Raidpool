# Changelog

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
