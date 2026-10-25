# Deterministic transcript wake activation

## Product role

This slice adds a usable local wake-activation fallback for the Living Agent path:

`speech-to-text transcript -> deterministic wake phrase match -> activation evidence`

It deliberately does not claim an acoustic wake-word model, always-listening microphone capture, speaker identity, or privileged-command authorization. A later measured acoustic detector can replace the transcript source without changing the activation safety semantics.

## Default behavior

The default wake aliases are the exact word tokens `ніка` and `nika`.

Matching is Unicode NFKC-normalized and case-insensitive. Punctuation around a wake word is allowed, but arbitrary substrings do not activate. For example, `Ніка, продовжуй` activates while `механіка` and `NikaCore` do not.

A custom policy may define up to 16 bounded single- or multi-word wake phrases. Phrases that collapse to the same normalized token sequence are rejected so policy order cannot hide duplicate activation authority.

## Safety and privacy

- transcript input is bounded to 4096 Unicode characters;
- control characters fail closed;
- request identities use a bounded safe identifier grammar;
- matching is pure and deterministic: no network, model, LLM, scheduler, memory store, or external effect;
- evidence stores only request identity, outcome, token positions/count, and SHA-256 bindings for the exact transcript and matched phrase;
- raw transcript text is not copied into evidence;
- activation by itself is not permission to execute a privileged action;
- speaker verification and any action-specific confirmation policy remain separate authorities.

## Composition boundary

The current implementation consumes text and therefore can compose after the existing STT lineage when that lineage is integrated. It does not edit or duplicate STT, TTS, speaker verification, microphone capture, or model-runtime code.

For lower-power or always-listening operation, a future acoustic wake detector should be evaluated on real Windows hardware and the intended languages/voice before promotion. Until that evidence exists, this slice must not be reported as physical acoustic wake-word support.

`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`  
`PHYSICAL_MICROPHONE_TESTED=false`  
`ACOUSTIC_WAKE_MODEL_PROVEN=false`  
`PRODUCTION_RELEASE_READY=false`
