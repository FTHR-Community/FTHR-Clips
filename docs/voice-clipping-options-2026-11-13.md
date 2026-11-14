# Two-phrase voice clipping: architecture and engine options

Research date: 13 November 2026. This is a design report, not a shipped plugin or a benchmark of trained FTHR models. The exact two sentences, including the Easter egg, remain intentionally undecided.

## Recommendation

Ship exactly two developer-selected phrases, recognized locally by dedicated wake-phrase detectors. Do not expose a phrase editor, user training, arbitrary keyword files, model import, or general transcription. Both detections should invoke the same existing clip-save action; any Easter-egg presentation should be a separate, fixed product decision.

The fixed vocabulary changes the earlier recommendation: **compare microWakeWord and openWakeWord for the production detector**, keeping sherpa-onnx as a faster-to-configure reference implementation. Select the smallest runtime that passes the same real gaming-audio reliability tests. Do not choose solely from upstream accuracy or Raspberry Pi benchmarks: neither establishes performance for FTHR's two sentences on Windows.

## Candidate engines

| Candidate | Why it fits | Integration cost and limitation | Proposed role |
|---|---|---|---|
| [microWakeWord, OHF-Voice](https://github.com/OHF-Voice/micro-wake-word) | Quantized streaming wake-word models designed for low-power devices; Apache-2.0 code. | Custom model training is an expert task. FTHR needs a native Windows inference wrapper and the matching audio frontend; the training package is not a desktop plugin. Check each model and training dataset separately. | First candidate for the smallest production footprint. |
| [openWakeWord](https://github.com/dscripka/openWakeWord) | Windows ONNX support, shared feature extraction across phrase models, and a documented custom-training path. | More runtime machinery than the microcontroller-oriented approach. Included pretrained phrase models have CC BY-NC-SA terms despite Apache-2.0 code. Use appropriately licensed custom models/data and audit all shipped assets. | Practical reference and fallback if it meets the performance budget. |
| [sherpa-onnx keyword spotting](https://k2-fsa.github.io/sherpa/onnx/kws/index.html) | Can restrict recognition to exactly two phrases without training new acoustic models; thresholds can be tuned per phrase. | Its open-vocabulary flexibility is unnecessary for the final user experience, and runtime/model cost must be measured. Fixed configuration must be enforced by FTHR, not exposed as editable keywords. | Rapid feasibility comparison, particularly before custom models are ready. |
| [Picovoice Porcupine](https://github.com/Picovoice/porcupine) | Windows support and a product aimed at a few fixed voice commands. | Uses an AccessKey and vendor model-generation tooling. Confirm distribution, activation/offline behavior and commercial terms before selecting it; a public repository is not proof of unrestricted engine redistribution. | Commercial alternative if its integration and terms are acceptable. |

[Vosk](https://alphacephei.com/vosk/models) and [whisper.cpp](https://github.com/ggml-org/whisper.cpp/tree/master/examples/stream) are lower priorities here. Their broader speech-recognition capabilities are unnecessary for two fixed phrases. Vosk documents roughly 300 MB runtime memory for a typical small model; Whisper's streaming example performs recurring transcription. They become more relevant if a separate future feature needs captions or transcript search.

## Keeping the performance impact low

Use one capture input, one resampler, one shared feature frontend where supported, and a single bounded inference worker. Reuse the microphone feed when available. Convert the existing 48 kHz mono feed to the selected model's required format, commonly 16 kHz mono. Keep inference, resampling and allocations out of the real-time audio callback: it should copy into a bounded queue and return. Timestamp every block; on overflow, discard stale data and reset detector state rather than allowing delayed saves.

Prefer CPU execution for these small models initially. Avoid waking a discrete GPU or competing with game rendering merely to evaluate two phrases. Start with one inference thread and no busy polling. For an ONNX candidate, explicitly evaluate disabling intra/inter-op thread spinning; ONNX Runtime documents that spinning trades extra CPU/power for latency. Validate the latency effect instead of assuming it is free. [ONNX Runtime thread configuration](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)

Do not start with continuous full transcription, a second denoising model, or speaker identification. Add such work only if it demonstrably improves the measured failure cases. An energy gate or voice-activity detector can help, but aggressive gating can cut off quiet phrase beginnings. Retain enough audio pre-roll and compare ungated detection against gated detection. A speech gate alone will not reject teammates or spoken game dialogue.

Load the models once when enabled and replay capture is available. Sleep on audio arrival rather than polling. Stop listening and release resources when disabled; suspend or reset across microphone changes, sleep/wake and unavailable capture. Do not create a new Python process or launch FFmpeg for every audio block.

## Reliability without custom phrases

Choose distinct, uncommon, reasonably short sentences with clear pronunciation. The Easter egg must be assessed acoustically like the main command: avoid a phrase that appears frequently in games, shares most of its sounds with the primary phrase, or requires an excessively long detection window. A long sentence may need a different model window and additional training, so phrase wording must precede final model selection.

Train with varied voices, accents, speaking rates, microphone quality, room reverberation, keyboard clicks, fan noise and game audio. Include similar-sounding noncommands and partial sentences as negative examples. Keep speakers and sessions in the evaluation set separate from training, and include consented real recordings; synthetic training alone is not evidence of real-world reliability.

Require confidence sustained across the detector's recommended observation window, then emit exactly one event per utterance. Use a short shared cooldown and require the detector to return below its reset threshold before rearming. Decide how to handle both classifiers firing on one utterance; never save two clips for the same event. Tune thresholds separately for the two models. The [ESPHome microWakeWord integration](https://esphome.io/components/micro_wake_word/) explicitly documents the latency/false-activation trade-off of cutoff thresholds and sliding windows.

Microphone-only input is the default. Avoid desktop-loopback audio; teammates may still bleed through speakers, so include that in the negative-audio evaluation. A fixed optional success sound must not trigger the detector itself. Confirm success from the existing save-completion event rather than treating recognition or an accepted request as a saved file.

The supported product can expose only two approved phrases and fixed phrase IDs. It cannot guarantee that a locally modified application or an edited social-media recording is impossible. The practical brand protection is a clear, fixed official feature without user-controlled trigger text or arbitrary transcription shown in the UI.

## FTHR integration findings

The current `PluginContext` provides settings, microphone-device resolution and effect hooks, but no owned streaming-sample subscription. `MicRecorder` has a shared 48 kHz mono buffer and RMS listeners, while Gary Mode uses level readings. Add an ownership-aware PCM subscription that cannot stop another feature's microphone stream; verify its behavior when the native capture backend owns audio. Do not infer live PCM availability from the level-meter API.

The Stream Deck bridge already maps `save_clip` to `_on_hotkey_save_clip`. A host-owned `request_clip_save` service would let both plugins share capture-readiness checks, duplicate suppression and UI-thread dispatch without depending on private window methods. Detectors should return only `primary` or `easter_egg`, confidence and a monotonic timestamp. Reject other IDs in the official voice feature. Do not expose arbitrary plugin commands through recognition.

RNNoise currently processes saved-clip preview/export only. Its installer, enable/disable lifecycle and settings UI are useful patterns, but it does not supply a live denoised microphone feed.

The feature ZIP validator currently caps packages at 32 MiB and allows Python, raster images and limited `.rnnn`/`.txt` assets. It does not accept `.onnx`, `.tflite` or native libraries. Prefer a versioned, verified native speech runtime distributed with a host update or an explicit optional runtime package, plus a small approved two-model feature package. Extend validation for the exact required assets and hashes rather than simply allowing every binary file. Training tools do not belong in the installed app.

Keep recognition local with no stored transcript. Diagnostics need only model version, timing, queue overruns, phrase ID and save outcome; raw microphone recordings should not be collected by default. Listening must be visible and controllable. Final clip timing can initially reuse the configured replay duration. Excluding the spoken command itself requires a separately specified timestamp-aligned end offset; it is not automatically achieved by keyword detection.

## Proposed release gates

These are proposed acceptance targets, not observed results or guarantees:

| Area | Initial target and test |
|---|---|
| CPU | Aim for less than 5% of one logical CPU's time on the minimum supported test machine while continuously listening; report silence, speech and sustained noisy speech separately. Also report whole-system CPU so denominators are explicit. |
| Gaming | No repeatable average-FPS loss above 1%, and no material worsening of 1% lows/frame-time tails in paired capture-on runs. Establish run-to-run variance before attributing differences to the plugin. |
| Memory | Start with an incremental working-set target below 100 MB; prefer substantially lower if accuracy is comparable. Measure the whole integration, not just model-file size. |
| Detection delay | 95th percentile below 500 ms from the end of the phrase to the save request, with save completion measured separately. |
| Intended commands | At least 95% detection in the agreed normal headset/noise conditions, reported separately for each phrase and accent/noise group. Publish failures instead of hiding them in one average. |
| False triggers | Target fewer than one per ten hours of representative negative audio. Evaluate at least 100 hours, report counts and confidence intervals, and include similar words, commentary, music and teammate bleed. A handful of quiet minutes cannot establish this. |
| Lifecycle | No duplicate saves, stale delayed saves or hung microphone resources across enable/disable, device unplug, sleep/wake, capture stop/restart and overload. |

Run fixed-phrase reliability tests and paired game-performance tests on the same release candidate. Select the winning model after both pass. First define the two sentences, collect the evaluation audio, then train/configure and compare the candidates. This avoids committing to a tiny model that cannot reliably recognize the eventual Easter-egg sentence.
