# Changelog

## 0.2.0
- Energieverwaltung pro Platte: Dauerbetrieb (Standard), Herstellervorgabe oder Abschalten nach Zeit
- Dauerbetrieb: Standby-Timer aus (hdparm -S 0), Kopfparken aus (APM 254), zusätzlich alle 3 min
  ein kurzer Lesezugriff – hält auch USB-Gehäuse wach, die sich selbst abschalten
- SMART: Anläufe (Motor) und Kopf-Parkvorgänge, Warnung ab 300.000 Parkvorgängen, eigene Sensoren

## 0.1.0
- Erste Version: Platten- und SMART-Übersicht, DrivePool-Übernahme (nur lesend), Pool per
  mergerfs, Duplizierungs-Analyse pro Ordner, Soll-Duplizierung, Sensoren, Weboberfläche.
