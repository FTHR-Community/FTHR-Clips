# Kleiner AMD-Feldtest – 17. Oktober 2026

Dieser Kandidat ist lokal gebaut und geprüft, aber noch **nicht auf AMD-Hardware qualifiziert**. Der Test soll zuerst einen überschaubaren Desktop-Fall bestätigen. H.264/AMF-Start allein ist kein Encode-/Save-Erfolg. Standardmäßig mit normalen Benutzerrechten starten, nicht automatisch „Als Administrator“. Schutzsoftware nicht umgehen oder Ausnahmen anlegen. Bei Blockierung die genaue Meldung melden.

Er enthält den geprüften Einzeltextur-Ansatz aus Community-PR #87 (vxnsin/Vensin), mit einem auf acht GPU-Texturen begrenzten FFmpeg-Pool. Ob damit die Bildfehler bzw. der gemeldete SubmitInput-Fehler 18 auf diesem AMD-PC verschwinden, ist genau Gegenstand des Tests, keine bereits bestätigte Zusage.

Entwicklerpfad: `E:\FTHR-Windows-P0-AMF-20261017-01\delivery\FTHRClips\FTHRClips.exe`. Beim Tester nach vollständigem Entpacken entsprechend `FTHRClips\FTHRClips.exe` im neuen Paketordner starten. `BUILD-IDENTITY.json` im Paketwurzelverzeichnis bindet UI, Engine, FFmpeg und Helper an die tatsächlich ausgelieferten Hashes.

## 1. Baseline: Desktop, H.264/AMF, 1080p60

1. Eine alte FTHR-Instanz zuerst regulär über „Beenden“ schließen, einschließlich möglicher Tray-Instanz; niemals zwangsweise beenden. Das vollständige ausgelieferte Bundle in einen neuen Ordner entpacken und dessen `FTHRClips.exe` starten; nicht nur die EXE kopieren. Keine zweite FTHR-Instanz parallel starten. Paket-/Manifest-Hash und Startpfad notieren.
2. Desktop/Monitor ausdrücklich wählen; AMD/AMF, H.264, 1920×1080, 60 FPS, Replay 30 Sekunden. **Spielerkennung eingeschaltet lassen**: Gerade mit aktivierter Erkennung muss die ausdrückliche Desktop-Wahl die Quelle manuell festhalten. Ein zuvor gewählter Auto-Modus darf noch angezeigt sein; zwischen Desktop-Wahl und Spielstart Auto-Switch/Prompt-First nicht erneut anklicken oder damit neu autorisieren. Die aktive Quelle und Codec-Anzeige sowie den Ausgangszustand fotografieren.
3. Mindestens 40 Sekunden mit sichtbarer Bewegung und unterschiedlichen Farben laufen lassen, damit die vollen 30 Sekunden Historie gefüllt sind. Dann dreimal speichern, jeweils etwa 10–15 Sekunden Abstand. Dateinamen, angeforderte Dauer, Ergebnisanzeige und eventuelle Warnung notieren.
4. Clips abspielen: bewegtes Bild, richtige Farben, keine schwarzen/alten Frames, richtige Quelle und vollständiger Anfang/Ende. Tatsächliche **Video-Präsentationsdauer** prüfen lassen; Audio-/Containerdauer ersetzt sie nicht. „Datei erstellt“, „partial“ und „full“ sind drei unterschiedliche Aussagen. Ein kürzerer Clip darf nicht als voller 30-Sekunden-Erfolg gezählt werden. Ein rechtmäßig als partial gemeldeter Clip ist kein Full-Pass.
5. Während Desktop-Capture ein Spiel starten, während die Spielerkennung weiterhin aktiv ist. Die vorherige ausdrückliche Desktop-Wahl muss erhalten bleiben, solange der Nutzer den automatischen Quellwechsel nicht erneut ausdrücklich wählt; der Spieltitel darf Metadaten ändern. Nicht automatisch Desktop→Fenster wechseln oder einen alten HWND reaktivieren.

## 2. Explizites Fenster und Wiederherstellung

1. Ein sichtbares Testfenster ausdrücklich wählen. Resize, minimieren, wiederherstellen. Quelle/Identität beobachten: kein stiller Desktop-Fallback, kein anderes Fenster bei wiederverwendetem Handle, keine alte Größe/Generation als scheinbar aktuelles Bild.
2. Während Minimierung/Neuaufbau können neue Frames fehlen; einen solchen Save nicht als Full-Erfolg voraussetzen. Fehler-/Pause-/Recovery-Anzeige und Verhalten dokumentieren.
3. Nach Wiederherstellung mit bewegtem Inhalt auf echte frische Frames warten, dann erneut mindestens 40 Sekunden für die volle Replay-Historie. Erneut speichern, Anzeige gegen tatsächliche Video-Dauer und Bildinhalt prüfen. Falls die Anwendung eine neue Capture-Generation verlangt, kontrolliert über die UI neu starten und nochmals vollständig aufwärmen.

## 3. Bei einem AMF-Fehler

Eine klar begrenzte Reproduktion genügt: genaue Aktion, Uhrzeit, gewählte Quelle/Geometrie, erste Fehlermeldung und Diagnosepaket sichern; nicht Dutzende Neustarts/Saves erzeugen. Ein FFmpeg-Fehler, ein natives AMF-Ergebnis und ein HRESULT sind getrennte Fehlerdomänen. `AMF_DIRECTX_FAILED` allein beweist weder die interne DirectX-Ursache noch einen Treiberfehler.

Erst wenn die native 1080p60-Baseline funktioniert, separat die ursprüngliche Feldgeometrie 1920×1080→2560×1440 bei 60 FPS prüfen. Neue Evidenzordner verwenden. H.265/AV1 und weitere Sonderfälle sind nicht Teil des ersten Baseline-Durchlaufs.

Zurückgeben: Kandidatenmanifest/Hashes, Diagnoseexport einschließlich `engine.log`, genaue Einstellungen/Aktionen/Uhrzeiten, alle tatsächlich versuchten Saves mit Full-/Partial-/Fehler-Ergebnis und die zugehörigen unveränderten MP4s. Ein nicht ausgeführter Versuch zählt weder als Erfolg noch als fehlgeschlagener Save. Persönliche Inhalte vor Weitergabe prüfen; nichts wird durch diese Anleitung automatisch hochgeladen.

## Optional: isolierter A/B/C-Tester

Der kompilierte Helper liegt im fertigen Paket unter `delivery/test-tools/amd-isolation-smoke.exe`; im entpackten ZIP ist das `test-tools/amd-isolation-smoke.exe`. Der Python-Runner daneben benötigt **Python 3.10+** (Standardbibliothek), aber mit `--helper` weder Visual Studio noch den vollständigen Quellbaum. Das ist kein vollständig eigenständiges Doppelklick-Testprogramm. FFmpeg/FFprobe und die passenden DLLs müssen aus demselben gelieferten, manifestgeprüften Runtime-Ordner stammen. `--ffmpeg-bin` ändert nur den Suchpfad der gestarteten Kindprozesse, nicht den System-PATH.

Beispiel aus dem vollständig entpackten Paketverzeichnis (dort liegen `FTHRClips` und `test-tools` nebeneinander). Der Ausgabeordner muss neu/nicht vorhanden sein. Der explizite Monitor-Gerätepfad wird vom Testbetreuer bereitgestellt, nicht aus einem Fenstertitel geraten.

```powershell
$MonitorPath = Read-Host 'Expliziter Monitor-Gerätepfad für Stage C'
python .\test-tools\qualify_amd_isolation.py --helper .\test-tools\amd-isolation-smoke.exe --ffmpeg-bin .\FTHRClips\_internal\engine --output "$env:USERPROFILE\Desktop\FTHR-AMD-native1080-01" --monitor-path "$MonitorPath"
```

A nutzt direkten D3D11-Input mit dem produktionsgleichen, begrenzten Einzeltextur-Pool, B den Produktions-Transform; beide prüfen nach Decode kontrollierte Farben, Bewegung und PTS. C prüft echte WGC-/SaveClip-Transport-/Decode-Evidenz, behauptet aber keinen kontrollierten Bildinhalt. Ohne `--monitor-path` wird C nicht ausgeführt und der Gesamtlauf ist nicht bestanden. Timeout oder `AMD_HARDWARE_REQUIRED` ist kein Pass. `result.json`, Logs und Medien zusammen aufbewahren.

Seit der Werkzeugänderung vom 18. Oktober müssen Runner und Helper gemeinsam neu gebaut bzw. geliefert werden. Mit dem ergänzten NV12-Vergleich verlangt der Runner Helper-Protokoll 3 und weist ältere Helper mit `helper_protocol_mismatch:rebuild_helper` vor den Aufnahmestufen ab. Die zuvor ausgelieferten Pakete enthalten dadurch nicht automatisch den neuen Helper.

Optional ergänzt `--compare-array` nach A/B/C einen separaten Lauf `a-array` mit dem alten BGRA-Texturarray. Dessen Ergebnis steht unter `array_control_status` und `stages["a-array"]`; es verändert `production_status` nicht. So kann ein defekter Array-Vergleich neben einer funktionierenden Einzeltextur-Produktion sichtbar bleiben. Das Gesamtformat hat jetzt `schema_version: 3`. Auch ein erfolgreicher Produktionslauf bleibt `PARTIAL`, solange kontrollierter Inhalt in C und weitere Hardware-/Audio-Prüfungen ausstehen.

Mit `--compare-nv12` startet anschließend an A/B/C ein getrennter BGRA-/NV12-Formatvergleich. Standardmäßig laufen zwei Runden in wechselnder Reihenfolge, bevor die gegebenenfalls angeforderte Array-Kontrolle beginnt. Ein fehlgeschlagenes Formatstadium stoppt weitere Formatversuche. Der Bericht unter `nv12_comparison` muss eigenständig geprüft werden; ein Exitcode 0 oder `production_status: PARTIAL` bestätigt keinen bestandenen NV12-Vergleich. Die Anwendung bleibt auf BGRA. Der Vergleich ist eine Diagnose und keine Freigabe des produktiven NV12-Pfads.

Runner und Helper müssen zum gleichen Protokollstand gehören. CI-/Buildprüfungen ersetzen keinen kontrollierten Radeon-Hardwarelauf.

Nach erfolgreicher Baseline denselben Aufruf mit **neuem** Ausgabeordner und zusätzlich `--field-geometry` wiederholen. Die Software erfasst tatsächliche Helper-/Runtime-/Log-/Medien-Hashes; fehlende Quelleninformationen im portablen Paket bleiben ausdrücklich unbekannt. Der Kandidatenmanifest-Hash verknüpft den ausgelieferten Helper mit dem geprüften Quellstand.
