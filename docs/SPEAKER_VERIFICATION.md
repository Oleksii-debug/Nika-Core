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
must match the bound provider, model, and requested enrolled-profile identity. Caller-controlled
route labels therefore cannot manufacture successful evidence.

Input is transient mono signed-16-bit little-endian PCM:

- 8 kHz through 48 kHz;
- 0.25 through 30 seconds;
- exact complete 16-bit samples;
- bounded request/profile identifiers;
- bounded timeout and pre-effect cancellation.

The adapter returns a finite confidence in `0..1`. The default deterministic policy is:

- `<= 0.60`: `NO_MATCH`;
- `0.60..0.85`: `UNCERTAIN` (exclusive of the endpoint rules above/below);
- `>= 0.85`: `MATCH`.

Thresholds are configurable as a strictly ordered bounded policy. A `MATCH` is still probabilistic
evidence, not identity certainty. A privileged command must combine this with wake/activation and
any configured confirmation policy.

## Privacy and evidence

Raw PCM and the raw enrolled-profile identifier are not copied into result evidence. Evidence keeps
only bounded route/model identity, confidence/outcome, audio size/timing metadata, a SHA-256 binding
of the transient audio, and a SHA-256 fingerprint of the profile identifier. Unknown adapter errors
are converted to a Nika-owned bounded message rather than exposing provider diagnostics.

No cloud fallback, network transport, model download, dependency, second memory store, scheduler,
ModelGateway, STT/TTS implementation, or UI is introduced by this slice.

## Next adapter work

A production adapter may later wrap an evaluated local speaker-embedding/verifier engine. That
adapter must own its provider/model identity and must preserve this service boundary; selecting a
specific vendor/model requires measured Windows/privacy/Ukrainian practicality evidence rather than
hard-coding one here.

`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`  
`PHYSICAL_MICROPHONE_TESTED=false`  
`PRODUCTION_RELEASE_READY=false`
