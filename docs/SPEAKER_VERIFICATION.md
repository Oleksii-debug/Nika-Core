# Local speaker verification boundary

## Purpose

This slice implements the replaceable, local-first **speaker verification boundary** required by the
Living Agent vision. It answers only a bounded probabilistic question: how strongly does this short
audio observation match an already-enrolled speaker profile?

It does **not** claim that a speaker is certainly Oleksii, execute privileged commands, capture the
microphone, detect the wake word, diarize a recording, persist a voiceprint, or choose an enrollment
workflow. Those remain separate product/policy capabilities.

## Contract

`SpeakerVerificationService` binds one adapter-owned `provider_id`, `model_id`, and LOCAL execution
kind at construction. The route is revalidated before and after every adapter effect, and a response
must match the bound provider/model, requested logical profile ID, and exact immutable enrollment
revision digest supplied by the enrollment authority. Caller-controlled route labels or stale
logical profile IDs therefore cannot manufacture reusable successful evidence for a different
voiceprint revision.

Input is transient mono signed-16-bit little-endian PCM:

- 8 kHz through 48 kHz;
- 0.25 through 30 seconds;
- exact complete 16-bit samples;
- bounded request/profile identifiers;
- a required lowercase 64-hex SHA-256 digest identifying the exact enrollment revision;
- bounded timeout and pre-effect cancellation.

The adapter returns a finite confidence in `0..1`. The default deterministic policy is:

- `<= 0.60`: `NO_MATCH`;
- `0.60..0.85`: `UNCERTAIN` (exclusive of the endpoint rules above/below);
- `>= 0.85`: `MATCH`.

Thresholds are configurable as a strictly ordered bounded policy. A `MATCH` is still probabilistic
evidence, not identity certainty. A privileged command must combine this with wake/activation and
any configured confirmation policy.

## Privacy and evidence

Raw PCM, raw voiceprint material, and the raw enrolled-profile identifier are not copied into result
evidence. Evidence keeps only bounded route/model identity, confidence/outcome, audio size/timing
metadata, a SHA-256 binding of the transient audio, a SHA-256 hash of the logical profile ID, and the
privacy-safe immutable enrollment-revision SHA-256 supplied by the enrollment authority. The adapter
must echo that exact revision and any mismatch or malformed digest fails closed. Unknown adapter
errors are converted to a Nika-owned bounded message rather than exposing provider diagnostics.

No cloud fallback, network transport, model download, dependency, second memory store, scheduler,
ModelGateway, STT/TTS implementation, or UI is introduced by this slice.

## Next adapter work

A production adapter may later wrap an evaluated local speaker-embedding/verifier engine. The
enrollment authority must expose a stable privacy-safe revision/artifact digest that changes whenever
the enrolled voiceprint material changes; the verifier must bind each result to that exact revision.
The adapter must also own its provider/model identity and preserve this service boundary. Selecting a
specific vendor/model requires measured Windows/privacy/Ukrainian practicality evidence rather than
hard-coding one here.

`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`  
`PHYSICAL_MICROPHONE_TESTED=false`  
`PRODUCTION_RELEASE_READY=false`
