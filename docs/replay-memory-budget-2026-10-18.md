# Speicherbudget für Windows-Replay und Video-Snapshots

Der Windows-Pfad begrenzt jetzt den Speicher für komprimierte Videopakete und ihre Save-Snapshots in Bytes. Mehrere Saves behalten ihre Reservierung auch nach dem Verlassen der Warteschlange, bis der jeweilige Worker seine Daten freigibt. Die Änderung gilt für AMF, NVENC, QSV und den expliziten Softwarepfad. Sie ist kein Nachweis, dass die gemeldeten AMD-Schwarzbilder aus älteren Builds behoben sind.

## Verhalten

Der bisher nur für die alte Rohbildkapazität ausgewertete Startparameter `max_buffer_mb` setzt unter Windows nun die gemeinsame Obergrenze für Videoring und lebende Video-Snapshots. Die UI übergibt 2048 MiB. Die Engine begrenzt den Wert zusätzlich auf die Hälfte des beim Start verfügbaren physischen Speichers beziehungsweise Commit-Spielraums, je nachdem, welcher Wert kleiner ist. Die Linux-Engine behält die bisherige Bedeutung ihres Startparameters.

Die Hälfte des so ermittelten Budgets steht der Aufnahme zur Verfügung, die andere Hälfte allen Saves zusammen. Ein zusätzlicher Save darf die Aufnahme-Reserve nicht aufbrauchen. Die Startprüfung schätzt den Bedarf für die eingestellte Historie einschließlich vier Sekunden Keyframe-Vorlauf, einer Sekunde Sicherheit, 25 Prozent Bitratenreserve, Metadaten und temporärem Paketwachstum. Zusätzlich muss ein vollständiger Save in den zweiten Anteil passen. Bei zu hohen Einstellungen verweigert die Engine den Start mit Bedarf, Budget und dem Hinweis, Cliplänge oder Bitrate zu reduzieren.

Die Schätzung ist keine Garantie über die VBR-Ausgabe eines Encoders. Zur Laufzeit werden die tatsächlich gehaltenen Paketkapazitäten belastet. Beim Wachstum eines Slots werden alte und neue Speicherblöcke berücksichtigt. Ein kleineres Paket gibt die weiterhin vorhandene Vektorkapazität nicht rechnerisch frei. Scheitert eine Reservierung, bleibt das bisher veröffentlichte Paket intakt. Die Engine stoppt die betroffene Aufnahme und meldet den Speicherfehler, statt mit einer Lücke weiterzuschreiben.

Vor einer Snapshot-Kopie reserviert die Auswahl ihren temporären Arbeitsspeicher und dann den vollständigen ausgewählten Videodatensatz. Ein zweiter oder dritter Save kann dadurch vor dem Kopieren seiner Paketdaten abgewiesen werden. Wartende oder bereits muxende Saves halten ihre Reservierung bis zur Freigabe des Snapshots. Rückgaben nach Auswahlfehlern, eine abgewiesene Queue-Übergabe und Shutdown geben die Reservierung wieder frei. Snapshots sind verschiebbar; eine implizite tiefe Kopie ohne neue Reservierung ist ausgeschlossen.

Bei voller Save-Queue erfolgt bereits vor der Snapshot-Erstellung eine Prüfung. Die abschließende Queue-Prüfung bleibt erhalten. Speicherfehler bei der weiteren Save-Vorbereitung oder im transaktionalen Writer liefern ebenfalls einen Fehler an die UI. Ein fehlgeschlagener Save meldet keinen Erfolg und beendet nicht den Save-Worker.

`Clear()` gibt Paketkapazitäten jetzt tatsächlich frei. Slot-Metadaten und Synchronisationsobjekte bleiben bestehen. Generationstrennung, Sperren gegen Überschreiben, Zeitstempel, Keyframe-Vorlauf und die bestehenden Replay-Health-Prüfungen bleiben wirksam. Die konfigurierte Historie wird bei Budgetproblemen nicht automatisch verkürzt.

## Diagnostik und Geltungsbereich

Die Startausgabe enthält `Video budget` und die Aufteilung in `ring_reserved` und `save_limit`. Laufzeitereignisse heißen `ring_memory_exhausted` und `snapshot_memory_rejected`. Sie enthalten belegte Ring-/Save-Bytes und die Gesamtgrenze. Die Snapshot-Auswahl kennt zusätzlich `MEMORY_BUDGET_EXCEEDED`.

Das ist eine Begrenzung der Videoring- und Video-Snapshot-Allokationen, kein Limit des gesamten Prozesses. Audio-Ringe und Audio-Snapshots, GPU-Texturen, Encoder-/Muxer-Arbeitsspeicher, weitere Diagnose-Strukturen und der Overhead des System-Allokators liegen außerhalb dieses Budgets. Sie müssen bei einer RAM-Messung zusätzlich betrachtet werden. Die bestehende Shared-Memory-ABI v4 wurde nicht erweitert.

## Validierung am 18.10.2026

Die produktiven Ring- und Queue-Quellen wurden lokal mit dem portablen Zig-0.14.1-C++-Compiler als Windows-Programme kompiliert und ausgeführt. Compiler und Testdateien liegen außerhalb des Projekts im temporären Verzeichnis. Die benötigte `avutil-60.dll` stammt aus dem im Repository gepinnten FFmpeg-Archiv; Archiv und DLL wurden gegen die vorhandenen SHA-256-Werte geprüft.

| Prüfung | Ergebnis |
| --- | --- |
| Neue C++-Speichertests | 110 Prüfungen bestanden |
| Bestehende Windows-Replay-Policy | 541 Prüfungen bestanden |
| Bestehende Ring-Dauerregressionen | Beide Szenarien bestanden |
| Acht gezielte Python-Testmodule | 112 bestanden, ein Test übersprungen |
| Shared-Memory-Vertrag | Windows und Linux unverändert gültig |
| Engine-Response-Vertrag | Bestanden |
| Python-Exception-Vertrag und Ruff | Bestanden |

Die Speichertests prüfen parallele Reservierungen, die Aufnahme-Reserve, zu große Startkonfigurationen, gehaltene Vektorkapazität, die Ablehnung eines weiteren Snapshots, unveränderte gespeicherte Daten während neuer Ring-Umläufe, Queue-Übergabe, aktiven Writer-Besitz, volle Queue und das Leeren von vier wartenden Saves nach Zerstörung des Rings. Auch frühe Auswahlausgänge und Recovery geben ihre Reservierungen frei.

Die neuen Tests sind in `FTHRclips_tests.vcxproj` eingebunden und werden damit vom zuvor ergänzten Windows-CI-Schritt ausgeführt. Der Linux-CMake-Test `windows_replay_memory` kompiliert dieselben Windows-Ring-/Queue-Quellen als portablen Vertragstest; er ändert den produktiven Linux-Ring nicht.

Die Python-Tests liefen mit Python 3.14.8, den gepinnten Entwicklungsabhängigkeiten und ohne die globalen Qt-Fixtures. Der übersprungene Test findet über seine bestehende Compiler-Suche kein `c++` oder `g++` im PATH; die oben genannten C++-Suiten wurden separat mit dem expliziten Zig-Pfad gebaut. Die vollständige Qt-Anwendung wurde hier nicht getestet.

Der vollständige MSVC-/Windows-SDK-Build bleibt ausstehend. Ein zusätzlicher Syntaxcheck des Engine-Einstiegspunkts endet an dem lokal fehlenden C++/WinRT-Header `winrt/base.h`. Die erfolgreichen portablen C++-Suiten sind daher kein vollständiger Build der Capture-Engine. Tests auf echter AMD-Hardware sowie RAM-Messungen unter Spiellast bleiben erforderlich.

Die lokalen Prüfprotokolle liegen unter `%TEMP%/fthr-amd-tests-20261018/`: `replay-memory.log`, `ring-duration.log`, `replay-policy.log`, die zugehörigen Build-Logs und `memory-python-tests.xml`.

## Nächste Hardwareprüfung

Nach einem vollständigen Windows-Build sind AMF-Aufnahmen mit langer Historie und hoher Bitrate sowie mehrere schnelle Saves unter Spiellast zu prüfen. Dabei müssen RAM-Verlauf, Paketfortschritt, tatsächliche Cliplänge, dekodierter Bildinhalt und Audio beobachtet werden. Ein Budgetfehler muss als Fehler sichtbar sein; ein erfolgreicher kurzer Desktop-Test belegt weiterhin keine stabile Spielaufnahme. Der BGRA-/NV12-Vergleich und die AMD-Karten-/Treiber-Matrix bleiben eigene Arbeitsschritte.
