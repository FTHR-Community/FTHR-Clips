# Encoder-Ausbau: Multi-Codec / Multi-Vendor / Presets P1–P7

**Datum:** 2026-06-01  
**Status:** Approved

## Ziel

Den bestehenden Encoder (nur H.264, NVENC/AMF/QSV/x264) auf alle drei Codec-Familien (H.264, HEVC, AV1) und alle gängigen Hardware-Encoder (Nvidia NVENC, AMD AMF, Intel QSV) sowie Software-Fallback auszubauen. Der User kann Codec und Preset in den Settings wählen; Standard ist automatische Auswahl des besten verfügbaren Encoders.

---

## 1. C++ Encoder (encoder.h / encoder.cpp)

### EncoderConfig — neue Felder

```cpp
enum class CodecPref { Auto, H264, HEVC, AV1 };

struct EncoderConfig {
    uint32_t src_width;
    uint32_t src_height;
    uint32_t enc_width;
    uint32_t enc_height;
    uint32_t fps;
    uint32_t bitrate_kbps;
    CodecPref codec_pref = CodecPref::Auto;  // neu
    int preset = 4;                           // neu: 1=schnellst … 7=beste Qualität
};
```

### Prioritätsliste (dynamisch aufgebaut)

`Open()` baut die Probier-Liste basierend auf `codec_pref`:

| Reihenfolge | Auto | H.264 only | HEVC only | AV1 only |
|-------------|------|------------|-----------|----------|
| 1           | av1_nvenc | h264_nvenc | hevc_nvenc | av1_nvenc |
| 2           | av1_amf   | h264_amf   | hevc_amf   | av1_amf   |
| 3           | av1_qsv   | h264_qsv   | hevc_qsv   | av1_qsv   |
| 4           | hevc_nvenc| libx264    | libx265    | libsvtav1  |
| 5           | hevc_amf  |            |            |           |
| 6           | hevc_qsv  |            |            |           |
| 7           | h264_nvenc|            |            |           |
| 8           | h264_amf  |            |            |           |
| 9           | h264_qsv  |            |            |           |
| 10          | libx264   |            |            |           |

### Preset-Mapping: ApplyPreset(ctx, codec_name, preset 1–7)

| Preset | NVENC | AMF | QSV | libx264/x265 | libaom/svtav1 |
|--------|-------|-----|-----|--------------|---------------|
| 1      | p1    | speed | veryfast | ultrafast | 0 (fastest) |
| 2      | p2    | speed | fast     | superfast  | 1 |
| 3      | p3    | balanced | medium | veryfast | 4 |
| 4      | p4    | balanced | medium | fast     | 6 |
| 5      | p5    | balanced | slow   | medium   | 7 |
| 6      | p6    | quality | slower  | slow     | 9 |
| 7      | p7    | quality | slowest | veryslow | 12 (best) |

> **AV1 Software:** Es wird zuerst `libsvtav1` versucht (schneller, bessere Qualität), dann `libaom-av1` als Fallback. Die Preset-Zahlen (0–12) sind SVT-AV1-spezifisch; libaom kennt kein numerisches Preset und wird ohne Preset-Option geöffnet.

### codec_used_out

`Open()` schreibt den vollen Codec-Namen (z.B. `"hevc_nvenc"`) in `codec_used_out` und zusätzlich in das neue Shared-Memory-Feld `active_codec`.

---

## 2. Shared Memory Protokoll

### Neue Felder (am Ende von SharedMemoryLayout anhängen)

```cpp
// C++ (shared_memory.h)
uint32_t cfg_codec_pref;   // 0=auto, 1=h264, 2=hevc, 3=av1
uint32_t cfg_preset;       // 1–7
char     active_codec[64]; // engine schreibt nach Open(), UI liest zum Anzeigen
```

Python-Seite (`capture_bridge.py`): identische Felder in `SharedMemoryLayout` (c_uint32 × 2, c_char × 64).

### Neue Methoden in CaptureBridge

```python
def set_encoder_config(self, codec_pref: str, preset: int) -> bool:
    # codec_pref: 'auto'|'h264'|'hevc'|'av1' → int 0–3
    # schreibt cfg_codec_pref + cfg_preset + sendet RECONFIGURE_ENCODER
    # Hinweis: RECONFIGURE_ENCODER muss noch im C++ Engine implementiert werden

def get_active_codec(self) -> str:
    # liest active_codec aus Shared Memory, gibt z.B. "hevc_nvenc" zurück
```

### Versionsupgrade

`FTHR_SharedMemory_v1` → `FTHR_SharedMemory_v2` (Layout-Änderung, alte Engine inkompatibel).

---

## 3. Settings

`~/.fthr/settings.json` bekommt zwei neue Keys mit Defaults:

```python
'codec_pref': 'auto',   # 'auto' | 'h264' | 'hevc' | 'av1'
'encoder_preset': 4,    # 1–7
```

Merge-Logik in `SettingsManager._load_settings()` übernimmt neue Keys automatisch für alte Config-Dateien.

---

## 4. UI — Performance-Tab (capture_settings_widget.py)

Neue Controls im Performance-Tab:

| Control | Typ | Werte |
|---------|-----|-------|
| Codec | QComboBox | Auto / H.264 / HEVC / AV1 |
| Preset | QComboBox oder QSlider | P1 (Schnellst) … P7 (Beste Qualität) |
| Aktiver Encoder | QLabel (read-only) | z.B. `hevc_nvenc — P4` |

**Verhalten:**
- Beim App-Start: Label wird aus `get_active_codec()` befüllt, nachdem Engine initialisiert ist.
- Beim Speichern: `set_encoder_config()` aufrufen → Engine rekonfiguriert sich via `RECONFIGURE_ENCODER`.
- Wenn Engine nicht verbunden: Label zeigt `"nicht verbunden"`.

---

## Dateien die sich ändern

| Datei | Änderung |
|-------|----------|
| `FTHRcapture_linux/src/encoder.h` | `CodecPref` enum + neue `EncoderConfig`-Felder |
| `FTHRcapture_linux/src/encoder.cpp` | Dynamische Prioritätsliste + `ApplyPreset()` + `RECONFIGURE_ENCODER` Handler |
| `FTHRcapture_linux/src/shared_memory.h` | 3 neue Felder am Ende des Structs |
| `FTHR_UI/core/capture_bridge.py` | Shared Memory Layout + 2 neue Methoden |
| `FTHR_UI/core/settings_manager.py` | 2 neue Default-Keys |
| `FTHR_UI/ui/capture_settings_widget.py` | Codec-Dropdown, Preset-Dropdown, Aktiver-Encoder-Label |

---

## Nicht in Scope

- Windows-seitiger C++ Encoder (separates Projekt, separate Session)
- AV1-spezifisches Tuning (ROI, film grain) — späteres Feature
- Bitrate-Anpassung pro Codec — bleibt wie bisher
