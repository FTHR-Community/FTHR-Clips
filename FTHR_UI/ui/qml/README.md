# GPU clip preview

VideoPreview.qml embeds Qt Multimedia VideoOutput in a QQuickWidget. Color
effects use a GPU layer with color.frag.qsb; sliders update shader uniforms.
Crop guides and stretch remain in the same scene. The existing player and
video sink stay attached throughout editing. No QVideoFrame.toImage() or
OpenCV processing runs for continuous GPU playback.

After editing color.frag, run `python tools/build_preview_shaders.py` from the
repository root. Commit/distribute the regenerated color.frag.qsb alongside
the source. Shader compilation is a development step, not a startup step.
The package includes D3D11 HLSL, SPIR-V, Metal and OpenGL variants.

FTHR.spec bundles this directory and the Qt Quick modules. Windows desktop
uses the GPU preview when available; offscreen/minimal test environments and
software scene graphs use the existing fallback. Runtime shader or scene
graph failure routes the existing sink frames to the bounded CPU preview.

Hardware verification on Windows:

```powershell
$env:QT_QPA_PLATFORM = 'windows'
python -m pytest tests/test_quick_video_preview.py
```

These tests compare shader output with CPU color math, check retained-frame
slider updates and vignette, and play a generated clip while adjusting sliders.
They verify unchanged player/source/sink identity, zero CPU frame conversions
during GPU editing, and continued playback after GPU failure. They do not
establish sustained high-resolution or high-frame-rate performance.

Export continues to use the existing FFmpeg filter chain. The shader matches
the existing SDR software preview, including its exposure/contrast/saturation
temperature ordering. The current preview and export algorithms are not
pixel-identical, and this change does not add HDR grading.
