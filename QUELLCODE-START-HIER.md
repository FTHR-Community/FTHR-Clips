# FTHR Clips 2.0.0-alpha - aktuelle Quellcode-Uebergabe

Stand: 17.10.2026. Diese ZIP enthaelt den aktuellen lokalen Arbeitsstand,
einschliesslich der noch nicht committeten Integration und Stabilisierung.
Es ist keine Kopie eines aelteren Git-Commits und kein fertiger EXE-Build.

## Fuer die Weiterentwicklung

- Die gesamte ZIP in einen neuen Ordner entpacken.
- UI und Anwendungslogik: `FTHR_UI/`.
- Windows-Capture/Playback: `FTHRcapture/`; gemeinsame Teile: `FTHRcapture_common/`.
- Linux-Capture: `FTHRcapture_linux/`; AppImage-Quellen: `AppDir/`, `build_linux.sh`.
- Ausserdem enthalten: optionale Dienste, Website, Assets, Tests, Build- und
  Dependency-Manifeste, Dokumentation und Lizenzdateien.
- Fuer Abhaengigkeiten, native Builds und Start aus dem Quellcode: `BUILDING.md`.
  Die dort dokumentierten verifizierten Downloads sind weiterhin erforderlich.
- Historische Notizen beschreiben ihren jeweiligen Zeitpunkt. Aktuelle Grenzen
  stehen in `KNOWN_ISSUES.md` und `RELEASE_NOTES.md`.

## Nicht enthalten

Keine Git-Historie, fertigen Programme/Installer, lokalen Build-Ausgaben,
Caches, virtuellen Umgebungen, persoenlichen Einstellungen, Aufnahmen oder
Diagnose-Logs. Benoetigte FFmpeg-Header/Importbibliotheken und UI-Shader bleiben
erhalten. Lose Scratch-Dateien und lokale Verknuepfungen sind nicht enthalten.

## Pruefbarer Snapshot

`SOURCE_MANIFEST.json` nennt Branch, Basis-Commit und SHA-256/Dateigroesse jeder
anderen Datei dieser ZIP. Alle Projektdateien sind bytegleich zum aktuellen
Quellordner; nur diese Uebergabe-Anleitung und das Manifest wurden hinzugefuegt.
Es wurde nichts committed oder auf GitHub gepusht.

## Offene Qualifikation

Diese Uebergabe ist keine oeffentliche Release-Freigabe. AMD-Hardwaretests sind
zurueckgestellt und nicht bestaetigt. Der bekannte Windows-Wiedergabeversatz
ist weiterhin offen; physische Mikrofon-Synchronisation ist nicht verifiziert.
Asset-/Lizenzstatus bleibt unveraendert; die ZIP erzeugt keine neue Freigabe.
