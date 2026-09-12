# Speaker diarization boundary

This slice implements the provider-neutral local speaker-diarization boundary required by the Living Agent runtime vision. It is deliberately smaller than a complete ambient-analysis feature and does not select or bundle a diarization engine.

## Runtime contract

The boundary accepts bounded transient mono/interleaved signed 16-bit PCM bytes plus an exact request identity. The configured adapter owns immutable local `provider_id` / `model_id` capability identity. Nika verifies that identity before and after the adapter effect and rejects route movement fail-closed.

The adapter response must bind the exact source-audio SHA-256 and return an immutable chronological tuple of timestamped speaker segments. Nika validates:

- source-audio identity;
- route identity;
- segment timestamps against source duration;
- bounded segment and distinct-speaker counts;
- bounded safe adapter speaker labels;
- finite optional confidence and latency values;
- declared overlap capability before accepting overlapping segments.

Raw provider speaker labels are not exposed by the public result. They are deterministically mapped to per-request numeric speaker indices in first-seen order. Durable/reportable evidence contains only route identity, exact audio/timeline digests, bounded counts, duration/sample metadata, overlap truth and latency. The raw audio bytes and raw provider labels are excluded from evidence.

## Failure semantics

The boundary is local-only. It has no cloud fallback and does not route through `ModelGateway`.

Unknown adapter exceptions and adapter-originated error text are minimized at the Nika boundary. A provider cannot forge Nika request/response/route error taxonomy by throwing a `DiarizationError`. Explicit unavailable and timeout states remain typed; caller cancellation propagates rather than becoming fabricated evidence.

Numeric policy and response fields reject booleans, NaN, infinities and huge values that cannot be converted safely. Resource bounds are checked before the adapter effect where possible.

## Composition boundary

This module intentionally does not edit or replace:

- speech-to-text (`#814` lineage);
- speaker verification (`#815` lineage);
- wake activation (`#816` lineage);
- speech synthesis / TTS (`#546` lineage);
- microphone capture;
- autobiographical memory;
- scheduler / Product Factory;
- ModelGateway;
- training runtime or model weights.

Future local engines such as a reproducible diarization model may implement `SpeakerDiarizerAdapter`. A later communication-coaching or ambient-analysis feature may compose this timeline with STT timestamps and speaker-verification evidence without changing this provider-neutral boundary.

## Acceptance truth

The included deterministic tests use fake local adapters only. They prove contract behavior, privacy minimization, exact route/audio binding, resource ceilings, overlap semantics, bounded timeout/cancellation and hostile-provider failure handling.

They do **not** prove a physical microphone, real acoustic diarization quality, enrolled-speaker identity, Windows audio-device behavior, human accessibility, or NVDA behavior.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

`PHYSICAL_MICROPHONE_TESTED=false`

`REAL_DIARIZATION_MODEL_PROVEN=false`

`PRODUCTION_RELEASE_READY=false`
