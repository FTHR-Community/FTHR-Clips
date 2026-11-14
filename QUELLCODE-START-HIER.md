# FTHR Clips 2.0.0-alpha - aktuelle Quellcode-Uebergabe

Stand: 19.10.2026. Diese ZIP enthaelt den aktuellen lokalen Arbeitsstand,
einschliesslich der noch nicht committeten Integration und Stabilisierung.
Es ist keine Kopie eines aelteren Git-Commits und kein fertiger EXE-Build.

Neuester Nachtrag: Alle vier vxnsin-PRs #87–#90 wurden abgeglichen. Der AMF-Fix war enthalten; Installer-Abbruch/Schrittanzeige, benannte Audio-Geraetespuren fuer Windows-Replay-Clips und Fenster-Resize-Recovery wurden ergaenzt. Bericht: `docs/vxnsin-pr-integration-2026-10-19.md`. Neue Kandidaten liegen separat unter `C:\Users\Tom\Documents\FTHR-vxnsin-PRs-20261019`. Bisherige AMD- und Audio-Sync-Fixes bleiben enthalten.

Audio-Sync-Nachtrag: Windows-MP4-Wiedergabe fuer Video und AAC korrigiert; neue Saves/Exporte und alte Clips ueber Wiedergabekopien angebunden. Das Audio-Mixing erhaelt das Videoende, der optionale Process-Loopback nutzt eine ueberlaufsichere Clock. Messungen, Tests und Grenzen stehen in `docs/windows-audio-sync-fix-2026-10-19.md`. Portable Testversion und Medien liegen separat unter `C:\Users\Tom\Documents\FTHR-Audio-Sync-20261019`. Mikrofon/Lautsprecher, lange Spieleaufnahmen und Radeon-Hardware sind weiter offen.

Nachtrag zur AMD-Abschlusspruefung: Die vorhandenen AMD-Aenderungen sind unter
`docs/amd-completion-review-2026-10-18.md` zusammengefasst und weitergeprueft.
Der zuvor offene vollstaendige Windows-Build wurde inzwischen ausgefuehrt.
Hauptmonitor-Erkennung und Capability-Cache wurden ergaenzt; ein eigener
Fenster-Lifecycle-Pruefer ist hinzugekommen. Exakte Ergebnisse stehen im
Bericht und seiner Evidenz-JSON. Echte Radeon-Aufnahmen bleiben ausstehend.

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

Das Manifest in der neuen ZIP nennt den urspruenglichen Basis-Snapshot und
SHA-256/Dateigroesse jeder anderen archivierten Datei. Der entpackte Arbeitsordner
hat keine Git-Metadaten fuer eine neue Branch-/HEAD-Angabe. Die archivierten Quelldateien sind bytegleich zum aktuellen
Quellordner. Das Manifest beschreibt dieses Inventar einschliesslich der
aktualisierten Dokumentation und der neuen Pruefwerkzeuge.
Es wurde nichts committed oder auf GitHub gepusht.

Die neuesten Aenderungen und Testresultate stehen in
`docs/vxnsin-pr-integration-2026-10-19.md` und der zugehoerigen Evidenz-JSON.
Der Audio-Sync-Bericht vom selben Tag bleibt separat erhalten.
Die AMD-Abschlusspruefung vom Vortag bleibt separat dokumentiert.
Die vorherigen Intel-Messungen bleiben im Bericht zu Quellenwechseln und
GPU-Probe erhalten. Build-Ausgaben, temporaere Testumgebungen, Rohprotokolle
und Caches gehoeren nicht zur Quellcode-ZIP. Ein erneuter Testlauf benoetigt
die in `BUILDING.md` beschriebenen Abhaengigkeiten.

## Offene Qualifikation

Diese Uebergabe ist keine oeffentliche Release-Freigabe. AMD-Hardwaretests sind
zurueckgestellt und nicht bestaetigt. Der bekannte Windows-Wiedergabeversatz
ist weiterhin offen; physische Mikrofon-Synchronisation ist nicht verifiziert.
Asset-/Lizenzstatus bleibt unveraendert; die ZIP erzeugt keine neue Freigabe.
