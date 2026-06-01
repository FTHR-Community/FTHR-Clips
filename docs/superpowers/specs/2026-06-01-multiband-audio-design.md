# Multiband Audio Design Spec

**Datum:** 2026-06-01  
**Status:** Approved

## Ziel

Per-App Audio-Kategorien mit konfigurierbaren Lautstärke-Presets. Beim Clip-Save werden alle Kategorien mit ihren Preset-Volumes sofort eingebrannt — kein separater Export-Schritt nötig. Das Feature ist komplett deaktivierbar für Performance.

---

## 1. Kategorien & Settings

### Neue Settings-Struktur

`~/.fthr/settings.json` bekommt zwei neue Keys:

```json
"multiband_audio_enabled": true,
"audio_categories": [
  {"name": "Game",    "volume": 100, "patterns": []},
  {"name": "Discord", "volume": 100, "patterns": ["discord", "Discord", "WebRTC"]},
  {"name": "Browser", "volume": 100, "patterns": ["firefox", "chrome", "chromium", "brave"]},
  {"name": "Musik",    "volume": 80,  "patterns": ["spotify", "Spotify", "vlc", "mpv"]},
  {"name": "Sonstige", "volume": 100, "patterns": []}
]
```

**Felder pro Kategorie:**
- `name` (str): Anzeigename, frei wählbar
- `volume` (int 0–100): Preset-Lautstärke, wird beim Clip-Save angewendet
- `patterns` (list[str]): Prozessname-Patterns für automatische Heuristik-Zuweisung. Leere Liste = Kategorie existiert aber keine App wird automatisch zugewiesen.

**Migration:** Beim ersten Start mit der neuen Version werden die alten `source_volumes` Keys (`game`, `browser`, `music`, `discord`) in `audio_categories` überführt. Die alten Keys bleiben als Fallback für ältere App-Versionen erhalten.

**Default bei deaktiviertem Multiband:** `multiband_audio_enabled: false` → bestehender Single-Track-Loopback bleibt aktiv, `audio_categories` wird ignoriert.

---

## 2. C++ AudioMultiCapture

### Neue Klasse: `audio_multi_capture.h` / `audio_multi_capture.cpp`

Die bestehende `AudioCapture` bleibt unverändert (Fallback/Singletrack-Modus). `AudioMultiCapture` ist vollständig unabhängig davon.

### API

```cpp
namespace fthr {

struct AudioCategoryConfig {
    std::string name;          // z.B. "Discord"
    std::string sink_name;     // interner PA-Sink-Name, z.B. "fthr_discord"
    std::vector<std::string> patterns;  // Prozessname-Patterns
};

class AudioMultiCapture {
public:
    static constexpr int kSampleRate = 48000;
    static constexpr int kChannels   = 2;
    static constexpr int kMaxSeconds = 120;

    bool Start(const std::vector<AudioCategoryConfig>& categories);
    void Stop();
    bool IsRunning() const;

    // PCM für eine Kategorie, identisches Interface wie AudioCapture::ExtractSegment
    std::vector<float> ExtractSegment(
        const std::string& category_name,
        int64_t end_time_ns,
        uint32_t duration_ms) const;

    // Welche laufenden Sink-Inputs wurden welcher Kategorie zugeordnet?
    // Format: { "firefox.exe" -> "Browser", "discord" -> "Discord" }
    std::map<std::string, std::string> GetCurrentMappings() const;
};

} // namespace fthr
```

### Startup-Sequenz

1. PA async mainloop + `pa_context` aufbauen
2. Pro Kategorie: `module-null-sink` laden (via `pa_context_load_module`) mit Name `fthr_<category_sanitized>`
3. Pro Kategorie: `pa_stream` auf den Monitor-Source der Null-Sink öffnen (`PA_STREAM_RECORD`), Samples in per-Kategorie-Ringbuffer (float32 stereo 48kHz, identisch zu `AudioCapture`)
4. PA-Subscription für `PA_SUBSCRIPTION_MASK_SINK_INPUT` setzen
5. Auf Sink-Input-Events: Prozessname aus `PA_PROP_APPLICATION_PROCESS_BINARY` lesen, Pattern-Match gegen alle Kategorien, `pa_context_move_sink_input_by_index` auf die passende Kategorie-Sink
6. Kein Match → Sink-Input wird der "Sonstige"-Kategorie zugewiesen (falls vorhanden) oder bleibt auf Standard-Sink (nicht im Clip). **Wichtig:** Im Multiband-Modus sind nur Apps im Clip hörbar die einer Kategorie zugeordnet wurden — unzugeordnete Apps sind stumm. Die "Sonstige"-Catch-All-Kategorie verhindert, dass Apps versehentlich verloren gehen.

### Shutdown

Beim `Stop()`: alle geladenen `module-null-sink` Module via `pa_context_unload_module` entladen. PA-Streams und Context ordentlich schließen.

### Integration in CaptureEngine

`capture_engine.h` bekommt:
```cpp
#include "audio_multi_capture.h"

class CaptureEngine {
    // ... bestehende Member ...
    AudioCapture            audio_;         // Singletrack-Fallback (unverändert)
    AudioMultiCapture       multi_audio_;   // Multiband
    bool                    multiband_enabled_ = false;
};
```

`CaptureEngine::Initialize` liest `multiband_enabled` aus `CaptureConfig`. Wenn true: `multi_audio_.Start()`, `audio_` wird **nicht** gestartet. Wenn false: `audio_.Start()` wie bisher.

`CaptureEngine::SaveClip` schreibt bei aktiviertem Multiband die Kategorie-PCM-Daten als temporäre WAV-Dateien neben den Clip (z.B. `clip_name_game.wav`, `clip_name_discord.wav`).

---

## 3. Clip-Save + ffmpeg-Mix

### Ablauf

1. `CaptureEngine::SaveClip` speichert den Videoclip wie bisher (Audio-Track: bei Multiband leer oder stiller Track, bei Singletrack der Loopback wie bisher)
2. Zusätzlich bei Multiband: eine temporäre WAV-Datei pro Kategorie neben dem Clip ablegen
3. Python liest nach Clip-Save die WAV-Dateien + die Preset-Volumes aus `audio_categories`
4. Einmaliger ffmpeg-Aufruf: N Kategorie-WAVs als Inputs, `amix` mit Volume-Weights, Output-Audio in den Clip einbrennen, Clip-Datei wird überschrieben
5. Temp-WAVs löschen

### ffmpeg-Kommando (Beispiel, 3 Kategorien)

```bash
ffmpeg -y \
  -i clip.mp4 \
  -i clip_game.wav -i clip_discord.wav -i clip_musik.wav \
  -filter_complex "[1:a]volume=1.0[g];[2:a]volume=1.0[d];[3:a]volume=0.8[m];[g][d][m]amix=inputs=3:duration=first:dropout_transition=0[aout]" \
  -map 0:v -map "[aout]" -c:v copy -c:a aac -b:a 192k \
  clip_mixed.mp4
```

Clip wird dann von `clip_mixed.mp4` zu `clip.mp4` umbenannt.

### Python-Integration

Neues Modul `FTHR_UI/core/audio_mixer.py` mit Funktion:
```python
def mix_multiband_clip(
    clip_path: str,
    category_wavs: dict[str, str],   # {"Game": "/tmp/clip_game.wav", ...}
    volumes: dict[str, float],        # {"Game": 1.0, "Discord": 1.0, ...}
    ffmpeg_exe: str
) -> bool: ...
```

`main.py` ruft `mix_multiband_clip` nach `save_clip` auf (analog zum bestehenden Mic-Mux-Pattern).

---

## 4. UI — Audio-Settings

Im `_make_audio_page()` in `main.py` kommt ein neuer Abschnitt **"Multiband Audio"**:

### Controls

| Control | Typ | Funktion |
|---------|-----|----------|
| Toggle "Multiband Audio" | QCheckBox | `multiband_audio_enabled` speichern, Abschnitt ein-/ausblenden |
| Kategorie-Liste | QVBoxLayout | Eine Zeile pro Kategorie |
| Pro Kategorie: Name-Label | QLabel | Anzeigename |
| Pro Kategorie: Volume-Slider | QSlider 0–100 | `volume` in `audio_categories` |
| Pro Kategorie: Löschen-Button | QPushButton | Kategorie entfernen |
| "+ Kategorie hinzufügen" | QPushButton | Inline-Formular: Name + Patterns (kommagetrennt) |
| Erkannte Apps | QLabel (read-only) | Aktualisiert via `bridge.get_audio_mappings()` alle 2s |

### Erkannte Apps

Das Label zeigt z.B.:
```
firefox → Browser ✓   discord → Discord ✓   spotify → Musik ✓
```

Dafür: neues Shared-Memory-Feld `active_audio_mappings[1024]` (JSON-String) das `AudioMultiCapture::GetCurrentMappings()` alle ~2s in `main.cpp` poll loop schreibt. Python liest und zeigt es an.

---

## Dateien die sich ändern

| Datei | Änderung |
|-------|----------|
| `FTHRcapture_linux/src/audio_multi_capture.h` | Neu: AudioMultiCapture Klasse |
| `FTHRcapture_linux/src/audio_multi_capture.cpp` | Neu: PA async Implementierung |
| `FTHRcapture_linux/src/capture_engine.h` | multi_audio_ Member + multiband_enabled_ |
| `FTHRcapture_linux/src/capture_engine.cpp` | Start/Stop multi_audio_, SaveClip WAVs schreiben |
| `FTHRcapture_linux/src/shared_memory.h` | `active_audio_mappings[1024]` + `multiband_enabled` (v3) |
| `FTHRcapture_linux/src/main.cpp` | argv[13]=multiband, poll loop mappings, CaptureConfig |
| `FTHRcapture_linux/CMakeLists.txt` | audio_multi_capture.cpp hinzufügen |
| `FTHR_UI/core/audio_mixer.py` | Neu: mix_multiband_clip() |
| `FTHR_UI/core/capture_bridge.py` | Layout v3, get_audio_mappings() |
| `FTHR_UI/core/settings_manager.py` | audio_categories defaults + Migration |
| `FTHR_UI/main.py` | UI-Abschnitt, post-save mix, argv[13] |

---

## Nicht in Scope

- Windows-seitiger Multiband-Capture (Windows Audio Session API — separates Feature)
- Echtzeit-Preview der Kategorie-Lautstärken während der Aufnahme
- Automatische Spielerkennung für Pattern-Matching (kommt mit Game-Detection Feature)
- Clip-Editor UI zum nachträglichen Remixen (kein separates Track-Speichern geplant)
