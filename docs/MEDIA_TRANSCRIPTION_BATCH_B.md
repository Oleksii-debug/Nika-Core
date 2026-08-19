# DEV05 Batch B — Audio + Resumable Transcription

Status: implementation candidate; not integrated until exact-head gates are green.

Starting main: `7ea619eec538d0da1fd79b7f64d96265ba746e18`.

## Practical capability

- external FFmpeg is still discovered/audited separately and never bundled by this batch;
- `FFmpegAudioExtractor` invokes only fixed validated argv through `SafeProcessRunner`, keeps `shell=False`/stdin disabled/process-tree cleanup, writes `.wav.partial`, then checksum-validates and atomically promotes;
- extraction supports deterministic bounded clips and normalizes to PCM16 WAV with metadata stripped;
- `inspect_pcm16_wav` explicitly classifies empty/silent normalized chunks without invoking ASR;
- `OfflineTranscriberPort` is Nika-owned and does not expose faster-whisper/sherpa objects;
- deterministic chunk plans persist start/end plus non-overlap core ranges;
- media schema v2 persists chunk state/results independently of process memory;
- completed chunks are immutable and skipped after restart; interrupted RUNNING chunks return to PENDING with explicit reconciliation evidence;
- chunk-local ASR timestamps are translated to global media timestamps before deterministic core-midpoint merge;
- `TranscriptionCoordinator` acquires the existing machine-wide `HEAVY_MODEL` resource lease before each non-silent ASR chunk and releases it in `finally`;
- faster-whisper and sherpa-onnx are optional peer adapters only. Missing packages/models are explicit errors; no model acquisition occurs in this path;
- benchmark harness records elapsed time, realtime factor, segment count and text size only. It does not select a winner.

## Upstream / license boundary

- faster-whisper engine code: MIT. Adapter constructs only from an explicit local model directory and sets `local_files_only=True`.
- sherpa-onnx engine package: Apache-2.0. Adapter requires explicit local encoder/decoder/tokens files.
- every speech model remains a separate `ModelDescriptor` with its own license reference/checksum; engine license never substitutes for model license.
- FFmpeg binary/build license remains build-dependent evidence from Batch A and is not inferred from the source-project default.

## Cancellation and restart truth

The coordinator is resumable at durable chunk boundaries. Completed chunks are never replayed. A process loss while a chunk is RUNNING causes that chunk to be re-queued on restart while earlier completed chunks remain immutable.

In-process ASR adapters do not currently claim proven hard mid-inference cancellation. Cancellation is therefore cooperative at chunk/process boundaries; this batch must not claim that faster-whisper or sherpa native inference has been forcibly stopped once an in-process decode call has begun.

## Resource truth

No engine/model is declared the target-laptop winner in CI. The target 16-GB machine keeps the existing `media_heavy_model/local_machine` concurrency limit of one. Physical Windows benchmarks are required before any default-engine choice or AMD-GPU acceleration claim.

## CI truth

Normal CI installs no faster-whisper, sherpa-onnx model, FFmpeg binary, or speech model. Deterministic tests use fakes for orchestration/restart/resource/silence behavior. Real-engine proof is a later focused/opt-in hardware step.

`HUMAN_TESTED=false`. `NVDA_VERIFIED=false`.
