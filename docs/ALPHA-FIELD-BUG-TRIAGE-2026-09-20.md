# FTHR Clips Alpha — anonymisierte Feldbug-Triage

Stand: 2026-09-20
Quelle: die bereitgestellte, zusammengefasste Alpha-Umfrage mit 51 Antworten
Scope: Triage und Abnahmekriterien; keine neue Produktdiagnose und keine
rechtliche Freigabe.

Diese Datei ist die aktualisierte und supersedierende Triage zur früheren
`docs/ALPHA-FIELD-REPORTS-2026-09-19.md`-Baseline. Die ältere Baseline bleibt
unverändert und fasst die vorherige 50er-Fassung nur auf hoher Ebene zusammen;
diese Fassung wertet die 51er-Rohumfrage neu aus und ordnet sie gegen den
aktuellen Linux-/Windows-Fixstand ein.

## Lesart und Beweislage

Die Rohdaten enthalten 51 externe Meldungen. Sie sind Feldberichte und wurden
nicht auf dem Entwicklerrechner reproduziert. Freitext, Hardwareangaben und
Logauszüge werden deshalb nur anonymisiert und ohne UserDiag-Links,
Personennamen oder lokale Benutzerpfade wiedergegeben. Gleichlautende
Meldungen wurden zu Fehlerklassen zusammengeführt; die Klassenanzahl ist
absichtlich nicht als Anzahl der betroffenen Personen interpretiert.

Eindeutig aus der Umfrage sind nur diese Aggregationen:

- 48 Meldungen treten laut Antwort "Every time" auf, eine "Sometimes", zwei
  "Not sure".
- Kategorien: 17 Capture/Replay, 2 Audio, 2 Editor/Export, 0
  Sharing/Uploads, 3 Hotkeys, 6 Installation/Updates, 10 Interface und 11
  Other. Kategorien können sich inhaltlich überschneiden.
- Es wurden 15 Log-/Screenshot-/Archiv-Anhänge angeboten; ihre Inhalte sind
  hier nicht als personenbezogene Evidenz übernommen.

Anonymisierte Umgebungsabdeckung der Rohmeldungen:

- 23 manuell beschriebene Windows-Systeme reichen von Windows 10 22H2 bis
  Windows 11 25H2. Genannt werden NVIDIA GTX/RTX, AMD Radeon RX 6000/7000/9000
  und Intel-Iris-Xe-Klassen sowie mehrere iGPU+dGPU-Konfigurationen; der
  angegebene Arbeitsspeicher reicht von 12 bis 64 GB.
- 17 manuell beschriebene Linux-Systeme verteilen sich auf Arch/CachyOS,
  Fedora/Bazzite, KDE Neon und Pop!_OS. Zwölf melden Wayland, drei X11 und zwei
  sind unsicher; genannt werden KDE Plasma, Hyprland, GNOME und Xfce sowie
  NVIDIA-, AMD-, Intel-Arc- und integrierte Vega-Grafik.
- Mehrere Berichte betreffen zwei Monitore mit gemischten Auflösungen und
  Bildraten, darunter 1080p, 1440p, 4K und Ultrawide. Das ist eine
  Feldabnahmematrix, kein Beweis für eine gemeinsame Monitorursache.
- Elf zusätzlich freigegebene externe Diagnoselinks wurden aus
  Datenschutzgründen nicht geöffnet oder in diese Datei übernommen.

Gegenüber der 19.09.-Baseline sind insbesondere folgende Punkte konkreter:

- Die Rohumfrage nennt jetzt explizit die 51er-Reproduzierbarkeitsangabe
  (48-mal "Every time"), die getrennten Kategoriegrößen und die aktuelle
  Plattform-/Compositor-Streuung.
- Die Meldungen werden in eigene Klassen für KDE/KWin-Portal, unpassende
  `hyprctl`-Anforderung, `BufferError` beim Linux-Mapping,
  `CAPTURE ADAPTER UNSUPPORTED`, H.264/AMF, AppImage-`AppRun`/GUI und den
  `linux_encoder_timestamps`-Gate gruppiert. Diese Details waren in der
  älteren Zusammenfassung nur zusammengezogen.
- Die aktuelle Roadmap-Abdeckung wird explizit getrennt von Feldbeweisen
  geführt: Portal/KDE und blank-UI bleiben feldoffen, RPATH ist implementiert
  und regression-getestet, der Timestamp-Test ist mit pinned FFmpeg
  implementiert und regression-getestet, und die glibc-Frage bleibt ein
  offener Release-Blocker.

Die Status-Spalte verwendet ausschließlich die vereinbarten Feldstatus.
"FIELD VALIDATION PENDING" bedeutet, dass lokale Tests oder eine Codeänderung
die externe Hardware-/Desktopklasse nicht ersetzen.

## Triage-Matrix

### P0 — Release- oder Kernpfad-Blocker

| Fehlerklasse | Status | Plattform / Klasse | Evidenz aus den Rohdaten | Aktuelle Fixabdeckung | Noch nötige Reproduktion, Diagnostik und Abnahme |
|---|---|---|---|---|---|
| Linux-AppImage startet ohne sichtbares Fenster, nur Taskleisten-Icon, leeres Fenster oder hängt | FIELD VALIDATION PENDING | Linux; Wayland/KDE, Fedora, CachyOS, Arch; auch AppImageLauncher | Mehrere Antworten melden kein GUI, ein leeres Fenster, Start nur im Panel oder Hängen; eine Meldung nennt zusätzlich `apprun` nicht gefunden | Launcher-/Runtime- und First-Paint-Pfade wurden gehärtet. Das ist lokal/regressionstechnisch abgedeckt, aber nicht auf den gemeldeten Desktops | Je eine FUSE- und `APPIMAGE_EXTRACT_AND_RUN=1`-Abnahme auf Fedora/KDE, CachyOS/Arch sowie X11/Wayland; sichtbares Fenster, zweiter Start, Close und keine Endlosschleife; Startdiagnostik muss Backend, Qt-Plattform und Exit-/Handshake-Fehler zeigen |
| KDE/KWin Wayland ohne funktionierende Bildschirmaufnahme; unpassende Hyprland-/Helper-Anforderung | FIELD VALIDATION PENDING | Linux; KDE/KWin Wayland, NVIDIA/AMD/Intel; Portal-/PipeWire-Topologie | Meldungen: "kein GUI"/kein Desktop-Capture, explizit Hyprland-Anforderung unter KDE; die bereitgestellte Logprobe zeigt wiederholte Engine-Initialisierung und `BufferError: cannot close exported pointers exist` | ScreenCast-Portal/PipeWire-Backend und gezielte Mapping-Freigabe sind implementiert und deterministisch regression-getestet; protocol-spezifische Backends bleiben erhalten. Der Startbericht kennzeichnet `hyprctl` nun als `feature-only` statt als globale Voraussetzung. Keine physische KDE-/GPU-Bestätigung behauptet | Portal-Auswahl, Restore-Token, Ablehnung, Abbruch, Monitorwechsel und 30-s Replay auf KDE Wayland; kein wiederholter Picker; nicht-schwarze Frames und gültiges Video; Diagnosebundle muss Backend, Portal-Fehler und Frame-Gesundheit enthalten |
| Capture-Engine startet nicht, verliert Verbindung oder meldet generisches Error 001 / CAPTURE BACKEND FAILED | FIELD REPORTED | Windows und Linux; Einzelmonitor, deaktivierter Monitor, Desktop-/Game-Capture | Mehrere Antworten melden Capture-Startfehler, Engine nicht verbunden, "nothing happens" oder Capture degraded; ein Bericht nennt einen deaktivierten Monitor | Native Fehler-/Adapterdiagnostik und Capture-Health-Instrumentierung sind vorhanden; die konkreten externen Ursachen sind nicht bewiesen und nicht pauschal behoben | Deterministische Fehler-Injektion für Engine-Handshake, Monitor disabled, Backend-Open und No-Frame; lokaler Smoke-Test; Feldbundle muss requested/actual monitor, Backend, Adapter, native call und Health-Phase zeigen; Acceptance: klare Subsystemklasse statt blindem Error 001 |
| Nur schwarzes/pitch-schwarzes Video oder nur Audio; korruptes, segmentiertes oder zeilenverschobenes Bild | FIELD REPORTED | Windows/Linux; Desktop und Spiele; mehrere Auflösungen/FPS/Monitore | Mehrere Meldungen: black screen, pitch black, audio-only, quadratisch segmentierte Replay-Daten und alternierende/verschobene Linien | Capture-/Encoder-Zähler und Save-/Replay-Diagnostik helfen bei der Trennung Capture → Encoder → Mux. Es ist keine vollständige Feldbehebung behauptet | Mit synthetischen Frames und deterministischem Encoder-/Mux-Failure testen; lokal 1080p/1440p und beide Backends smoke-testen; Feldabnahme: sichtbarer Vollframe, dekodierbarer Clip, A/V vorhanden, keine Duplikat-/Stall-Phase; Diagnose muss letzte erworbene/eingereichte/kodierte Zeitstempel ausweisen |
| Monitor, DXGI-Ausgabe und Capture-/Encoder-Adapter passen nicht zusammen | FIX IMPLEMENTED / REGRESSION TESTED — FIELD VALIDATION PENDING | Windows; Multi-GPU, iGPU+dGPU, mehrere Monitore, gemischte Ausgänge | Explizite Meldung `CAPTURE ADAPTER UNSUPPORTED` mit "selected monitor ... exact display adapter"; weitere Meldungen nur auf einem Monitor erfolgreich | Nichtleere gespeicherte Monitor-IDs werden bei Disconnect/Disable nicht mehr still auf Primary umgeschrieben; die UI zeigt das gespeicherte Ziel als nicht verfügbar und der native Resolver erhält die ursprüngliche ID. Adapter-/LUID-Kette bleibt instrumentiert. Das behebt die belegte Silent-Fallback-Ursache, nicht pauschal jede externe Hybrid-GPU-Ursache | Synthetic topology A/B, negative Koordinaten, deaktivierter Output und iGPU+dGPU sind regression-getestet; physisch auf Multi-GPU/hotplug abnehmen; requested vs actual chain muss vollständig sein und mismatch begründen statt still zu fallbacken |
| H.264-/AMF-/Hardware-Encoder kann nicht initialisiert werden | FIELD REPORTED | Windows; AMD AMF sowie gemischte NVIDIA/AMD/Intel-Adapter; auch Linux-Hardwarepfade | Mehrere Antworten nennen H.264 nicht erkannt, AMF-Initialisierung, Error 001 wegen nicht initialisiertem H.264 oder Hardware-Encoding `unknown`; mehrere Auflösungen/FPS | Native Encoder-/Adapterdiagnostik und AMF-Klassifikation sind vorhanden. Externe AMD-/Hybrid-Hardware ist nicht verfügbar; keine hardware-spezifische Behebung behauptet | Failure injection für NVENC/AMF/QSV, falschen Adapter und unsupported codec; reale AMD/NVIDIA/Intel Matrix; Acceptance: genaue Backend-/Codec-/LUID-Diagnose, kontrollierter Software-Fallback nur wenn vorgesehen, kein generisches Error 001 |
| Release-AppImage hat unklare glibc-/Qt-/FUSE-Kompatibilität | FIELD REPORTED | Linux; ältere glibc-Baselines bis aktuelle Fedora/rolling distributions | AppImage-Startfehler sowie "first/newest AppImage"-Varianten; die aktuelle Roadmap führt glibc-Kompatibilität als offenen Release-Blocker | Roadmap bleibt ausdrücklich **OPEN — RELEASE BLOCKER**; die RPATH-Anpassung löst keine glibc-Symbolanforderung | Pinned older build environment; ELF-glibc ceiling prüfen; FUSE und extract-and-run auf älterem Referenzsystem und Fedora; Qt Wayland/XCB laden; SHA-256 veröffentlichen |
| Antivirus/Installer blockiert den Download oder markiert den Build | FIELD REPORTED | Windows; Drittanbieter-Antivirus/Installer-Download | Eine externe Meldung sagt, der Download sei in Quarantäne als High Risk/Miscellaneous eingestuft worden | Keine stillschweigende technische Entwarnung; dies ist Release-Ops/Vertrauen und kein Capture-Code-Bug | Reproduzierbarer signierter Release-Build, Authenticode-/Publisher-Kette, VirusTotal/AV-Partner-Feedback mit Hash, klare Download- und Hash-Seite; erst danach Feldabnahme auf einem sauberen Testsystem |

### P1 — Zuverlässigkeit, Audio, UI und Build-Gates

| Fehlerklasse | Status | Plattform / Klasse | Evidenz aus den Rohdaten | Aktuelle Fixabdeckung | Noch nötige Reproduktion, Diagnostik und Abnahme |
|---|---|---|---|---|---|
| System-/Discord-/Mikrofon-Audio fehlt, stottert, knackt oder ist zeitlich falsch | FIELD VALIDATION PENDING | Windows/Linux; PipeWire/PulseAudio, Discord-Call, unterschiedliche Endpoints | Meldungen nennen fehlende Audio-Inputs nach Discord, Discord-only Stottern, Popping und defekte Audioaufnahme; Audio-Kategorie enthält 2 Antworten, Freitext überschneidet Capture | Audio-Timing-/Packet-Diagnostik und Windows-Audio-Grundlagen sind implementiert; reale Endpoint-, Process-Loopback- und Mikrofonqualität bleibt offen | Endpoint-/Device-Change- und Discord-Szenarien; 30/60-s und 10-min A/V-Messung; Packet-/Discontinuity-/Underrun-Zähler statt Audiodaten; Feldabnahme auf PipeWire/PulseAudio und Windows |
| Hoher RAM-/GPU-Verbrauch, Memory-Leak oder Wachstum im Idle/Replay | FIELD REPORTED | Windows/Linux; Idle-Replay, große Bibliotheken, Engine-Prozess | Mehrere Meldungen nennen 600 MB–1,3 GB im Idle, >1,2 GB nach kurzer Laufzeit, Engine mit vollem RAM und ca. 15 % GPU ohne erkannte Anwendung | Library-/Capture-Health-Telemetrie und Save-Timing sind vorgesehen bzw. instrumentiert; keine Ursache aus den Feldtexten ableitbar | 30-min Idle/Replay soak, bounded library scan, allocation/process RSS snapshots, GPU sampling; Acceptance: kein unbounded growth, definierte Grenzwerte, keine per-frame Logs |
| Windows-DPI/4K/150-%-Skalierung macht Fenster, Resize-Griff oder Controls falsch | FIELD VALIDATION PENDING | Windows; 4K/2K, 125–150 % DPI, maximiert, zweiter Monitor | Mehrere Antworten melden nur teilweise nutzbare UI, Resize-Cursor in der Fenstermitte, DPI-Kompatibilitäts-Workaround sowie Wallpaper-Rand oben/links | Frameless-/Scaling-/Monitor-Fallbacks wurden geändert und deterministisch getestet; physische 4K/125–150-%-Abnahme bleibt offen | 100/125/150 % mit Monitorwechsel, maximiert/freier Resize, negative Coordinates; Screenshot- und Click-target assertion; Feldabnahme ohne Kompatibilitäts-Override |
| Schaltflächen, Settings, Dropdowns und Game-Detect-Switches reagieren nur teilweise | FIELD REPORTED | Windows/Linux; Maus-/DPI-/Frameless-Interaktion | Mehrere Meldungen: Buttons funktionieren nicht, Settings nicht klickbar, Switches reagieren nur links, kleinere Clickboxen; Dropdown verhindert vorübergehend System-Hotkeys | Keine ausreichende Evidenz für eine einheitliche Root Cause; UI-Interaktion ist als eigener Fehlerbereich zu testen, nicht als Capture-Fix zu zählen | QTest-/UI-Klickmatrix für alle genannten Controls, DPI/zweiter Monitor/Dropdown-Fokus; Acceptance: gesamte Hitbox, Tastatur-/Mausfokus, keine globale Shortcut-Sperre nach Dropdown |
| F1–F8/F7/F8 lassen sich nicht speichern oder springen auf F9 zurück | FIX IMPLEMENTED / REGRESSION TESTED — FIELD VALIDATION PENDING | Windows; Hardware-/Layer-Tastaturen und globale Hotkeys | Mehrere Antworten melden F7/F8-Rebind, F1–F8 und Alt nicht möglich, F9-Fehler oder Rückfall auf F9; ein Bericht erwähnt eine Layer-Tastatur | F7/F8 waren korrekt, aber unsichtbar für Game Detection reserviert; die Duplicate-Rejection stellte daher F9 wieder her. `CONFIRM GAME` und `DISMISS GAME` sind jetzt sichtbar/editierbar. F1-F12, Alt-Kombinationen, Persistenz und sichtbare Konfliktmeldung sind deterministisch getestet. Layer-/fremde RegisterHotKey-Konflikte bleiben Feldthemen | Feldabnahme auf Standard- und Layer-Tastatur; Acceptance: gültige Taste speichert über Relaunch, alle belegten Aktionen sind sichtbar und externe Registrierungsfehler werden konkret angezeigt |
| Spiel-/Fenstererkennung und Capture-Auswahl verhalten sich falsch oder lösen Anti-Cheat-Sorge aus | FIELD REPORTED | Windows; Fortnite, Minecraft, CS2, Roblox, Valorant, R6S und andere Spiele/Apps | Mehrere Spiele wurden genannt; Game-Detect-Switches/Deaktivieren ändern Capture-Verhalten; eine Meldung befürchtet Exploit-/Anti-Cheat-Flags | Keine Spekulation über Anti-Cheat-Ursache; instrumentieren, welche Erkennung und welcher Capture-Modus tatsächlich gewählt wurde | Testmatrix mit Desktop, App und mehreren Fensterzuständen; keine Eingabe-Injektion; Acceptance: Auswahlzustand stimmt, Fehlermeldung erklärt fehlende Berechtigung/Backend, Security-Risiko separat reviewed |
| Linux-Hardware-Encoder wird als unknown/libopenh264 nicht verfügbar gemeldet | FIELD VALIDATION PENDING | Linux; Intel Arc, NVIDIA, AMD, software-only Umgebung | AppImage-Log nennt `libopenh264`/unknown Hardware-Encoding; Arc B580 meldet AppImage-Start-/Captureproblem | Fallback-/Encoder-Diagnostik vorhanden; VA-API/native hardware path und physische Intel/AMD/NVIDIA-Abnahme bleiben offen | Release-FFmpeg/VA-API probe auf echter GPU, software fallback, codec/backend fields; Acceptance: kein irreführendes "hardware active", klarer fallback und gültiger Clip |
| Linux native CTest schlägt bei `linux_encoder_timestamps` fehl | REGRESSION TESTED | Linux build; fehlende Hardware/Codec-Abhängigkeit in generischem Test | Externe Antwort zeigt 13/14 Tests und `linux_encoder_timestamps` subprocess abort | **FIX IMPLEMENTED + REGRESSION TESTED**: generischer Timestamp-Test nutzt Software-Encoder; pinned FFmpeg suite pass. Physische VA-API-/Feldvalidierung bleibt `FIELD VALIDATION PENDING` | Pinned suite in CI und echter VA-API-Test separat; bei Hardware-Fehlern Diagnose statt Testflakiness; keine Freigabe der physischen Matrix ableiten |
| AppImage-Build-/Qualifikationsmatrix ist nicht vollständig | INSTRUMENTED | Linux; distro/compositor/GPU/audio-Kombinationen | Meldung: AppImage kompilieren/testen schlägt; Umfrage enthält Wayland, X11, KDE, Hyprland, GNOME, Xfce, NVIDIA, AMD, Intel und PipeWire/PulseAudio | Report-/Checksum-Workflow, Portal-/RPATH-/native test gates sind vorhanden; Roadmap #6 bleibt **PARTIAL — MATRIX INCOMPLETE** | CI mit pinned toolchain, FUSE/extract fallback, Ubuntu/Debian plus rolling, X11/Wayland compositor matrix, GPU encoder/audio/replay/hotkey/screenshot; nur reale Ergebnisse als qualifiziert markieren |
| Linux Capture-Helper ist compositor-spezifisch unvollständig (X11/Wayland/Screenshot) | INSTRUMENTED | Linux; KDE, GNOME, Hyprland, Xfce, X11/Wayland | Reports verlangen `hyprctl` auf KDE bzw. scheitern ohne erwarteten Helper; Screenshot- und Replay-Pfad werden vermischt | Portal und protocol-specific capture sind getrennt; Screenshot-Fallback und native matrix bleiben laut Roadmap offen | Je Compositor getrennte Tests für Replay und Screenshot; fehlende Helfer müssen optional/verständlich sein; Abnahme mit tatsächlichem Screenshot und Replay |
| App startet oder engine verbindet nach Monitor-Deaktivierung nicht | FIX IMPLEMENTED / REGRESSION TESTED — FIELD VALIDATION PENDING | Windows; deaktivierter/neu aktivierter Monitor, Multi-monitor | Explizite Meldung: Engine verbindet nicht, wenn Monitor deaktiviert ist | Der konkrete Silent-Fallback-Defekt ist behoben: eine gespeicherte nichtleere Monitor-ID bleibt erhalten, wird als nicht verfügbar angezeigt und für native Diagnose/Resolution durchgereicht. Eine automatische Reconnect-Policy wurde nicht neu erfunden | Physische Disable/enable- und Hotplug-Abnahme, persistent monitor re-resolution und bounded reconnect; Acceptance: klare no-output state, kein stilles Primary-Fallback, Recovery nur mit gültigem Output |
| Cursor fehlt im Vollbild | FIELD REPORTED | Windows/Linux; Minecraft-Fullscreen | Eine konkrete Meldung beschreibt unsichtbaren Mauszeiger nur im Vollbild | Keine bestätigte technische Ursache oder Fixabdeckung | Fullscreen/borderless/windowed matrix mit mehreren backends und overlays; Acceptance: Cursor bleibt sichtbar oder UI erklärt bewusstes capture/lock Verhalten |

### P2 — Produktpfad und ältere/sekundäre Reports

| Fehlerklasse | Status | Plattform / Klasse | Evidenz aus den Rohdaten | Aktuelle Fixabdeckung | Noch nötige Reproduktion, Diagnostik und Abnahme |
|---|---|---|---|---|---|
| Clips bleiben in FINALIZING/Export hängen, bis die App neu gestartet wird | FIELD REPORTED | Windows/Linux; Replay speichern, Editor/Export | Mehrere Meldungen nennen dauerhaftes FINALIZING oder Hängen nach dem Clip | Lifecycle-/Export-Diagnostik und bounded process evidence existieren; keine End-to-end Feldbehebung behauptet | Failure injection für mux/finalize/process exit; ein Clip pro Job, sichtbarer FAILED/CANCELLED-Zustand, kein stilles Relauch-Erfordernis; output existence/size prüfen |
| Clip-Bibliothek findet vorhandene Dateien nicht oder skaliert schlecht | FIELD REPORTED | Windows/Linux; große Verzeichnisse/andere Laufwerke | Meldung: Explorer sieht Clips, FTHR findet sie nicht; weitere RAM-/Scanberichte betreffen große Bibliotheken und Laufwerke | Library-scan/cache diagnostics sind vorgesehen; keine automatische Komplettbehebung aus diesem Bericht | Bounded scan mit Zugriffsfehlern, anderen Laufwerken, Netz-/Symlink-ähnlichen Fällen und 125-GB-Simulation; Acceptance: sichtbarer Scanstatus, bounded memory, keine per-file privacy logs |
| Vertikale Linien/Frame-Verschiebung und segmentierte Replay-Dateien | FIELD REPORTED | Windows; 60 FPS, 720p/1440p, hohe Auflösung | Freitext nennt quadratische Segmentierung, alternierende Linien und "cyberpsycho"-Clips | Wird in der schwarzen/korrupten Video-Klasse analysiert; keine separate Reparatur behauptet | Deterministische stride/pitch/rotation tests, encoded frame checksum, 30/60-s physical clips at 720p/1080p/1440p; Acceptance: dimensions/stride correct and no tearing |
| Insight-Capture-Import fehlt | FIELD REPORTED | Produktfeature, nicht Linux-Capture-Bug | Mehrere Antworten wünschen Import vorhandener Clips aus Insight Capture und erwarten gefundene Clips | Separater Feature-/Upload-Roadmap-Punkt; nicht als behobener Bug ausweisen | Produktentscheidung zu unterstütztem Container/Metadata, Import safety, duplicate handling, test fixtures; keine Änderung im Rahmen dieser Triage |
| Edited-result upload nutzt möglicherweise falschen Pfad | FIELD REPORTED | Produktfeature/Upload; nicht in der Umfrage eindeutig reproduziert | Die 51er Rohdaten enthalten keine belastbare Upload-Meldung, die Ursache beweist | Roadmap #34 behandelt dies getrennt als nicht sichere Ein-Zeilen-Änderung | Erst exportierten Resultpfad deterministisch erfassen; Upload erst nach erfolgreichem Export; keine Feldfreigabe ohne testbare path assertion |
| Legacy-`Colors.BG_CARD`-Launchreport | FIELD REPORTED | Linux; alter 1.0.0-Artefaktbericht | Nicht als aktueller Rohbericht bewiesen; Roadmap führt ihn als veralteten Artefaktfall | Source-Symbol ist im aktuellen Baum nicht belegt; Roadmap verlangt Frozen-Bundle-Smoke-Test vor Schließen | Mit neuem AppImage starten, Bundle smoke-testen und erst danach schließen; nicht mit dem aktuellen blank-UI-Report vermischen |

## Abgrenzungen

### Release-Ops statt Produktbug

Antivirus-/Quarantäne-Meldung, Code-Signing/Publisher-Vertrauen, glibc ceiling,
FUSE/extract-and-run, AppImage-RPATH und die vollständige Distribution-/GPU-
Qualifikationsmatrix sind Release-Ops. Sie dürfen nicht als Capture-Fix oder
als Beweis für eine funktionierende App gewertet werden. Der RPATH-Punkt ist
bereits **FIX IMPLEMENTED + REGRESSION TESTED** (der Status in der Roadmap ist
entsprechend `IMPLEMENTED / REGRESSION TESTED`); glibc bleibt **OPEN — RELEASE
BLOCKER**.

### Feature-Wunsch statt Bugfix

Der Import von Insight-Capture-Clips ist ein Produktfeature. Er braucht eine
Entscheidung zu Container/Metadaten, Sicherheits- und Duplikatverhalten und
einen eigenen Testpfad. Er wird nicht durch Portal-, Encoder- oder Bibliotheks-
Fixes gelöst.

### Nicht als Feldvalidierung ausgeben

Die aktuelle Linux-Implementierung hat deterministische Portal-, Recovery-,
Audio-, RPATH- und pinned-FFmpeg-Tests. Das beweist nicht, dass eine externe
KDE-/AMD-/Intel-/NVIDIA-/Hybrid-Konfiguration funktioniert. Alle entsprechend
markierten Klassen bleiben bis zur neuen Alpha und einer Rückmeldung des
betroffenen Hardwaretyps `FIELD VALIDATION PENDING`.

## Abnahme-Reihenfolge

1. P0: reproduzierbarer, signierter und glibc-kompatibler AppImage-Release;
   Start auf älterer Distribution und Fedora; sichtbare UI.
2. P0: KDE/KWin Wayland über Portal, Desktop- und Monitor-Capture, nicht-
   schwarzer Replay und vollständige Adapterkette.
3. P0: Windows Capture-/Adapter-/Encoder-Fehler mit requested-vs-actual-
   Diagnostik und synthetischer Failure-Injection.
4. P1: Audio-Endpoint/Device-Change/A-V-Sync, DPI/UI/Hotkey-Matrix,
   Linux-GPU-Encoder und die native AppImage-Matrix.
5. P2: Finalisierung, Bibliotheksskalierung, Frame-Corruption und die
   Insight-Import-Entscheidung.

## Datenschutz und Follow-up

Diese Datei enthält keine UserDiag-URLs, Namen, lokalen Benutzerpfade,
Clipinhalte, Screenshots oder Audio. Künftige Feldberichte sollen stattdessen
das Diagnosebundle mit redigierten Pfaden, Session-ID, requested/actual
Konfiguration, Backend-/Adapterkette und bounded health counters liefern.
Die bestehende [Alpha-Fix-Roadmap](ALPHA-FIX-ROADMAP.md) bleibt die Quelle für
Issue-Tracking und Release-Gates; diese Datei ist die anonymisierte
Triage-Baseline der 51 Rohmeldungen.
