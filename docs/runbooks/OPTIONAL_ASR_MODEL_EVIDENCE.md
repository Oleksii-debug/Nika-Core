# Optional ASR local-model evidence

Status: MANUAL-DEV25 implementation runbook. This document does not grant M10 acceptance credit by itself.

## Ownership boundary

This batch is deliberately independent from active DEV05 PR #89. It does not edit remote acquisition, safe process execution, subtitle selection/materialization, or subtitle restart paths owned by that PR.

The batch hardens the already integrated optional ASR adapters in `src/nika_core/media/transcribers.py`:

- Faster-Whisper with an explicitly selected local model directory;
- sherpa-onnx Whisper with explicit local encoder, decoder, and token files.

Neither adapter installs an engine, downloads a model, reads browser profiles/cookies, or accepts a remote model identifier as a substitute for a local artifact.

## REUSE → ADAPT → CUSTOM(thin)

- **REUSE:** Faster-Whisper remains the upstream transcription engine; current upstream engine license is MIT.
- **REUSE:** its current native inference dependency CTranslate2 remains an upstream runtime dependency; current upstream license is MIT.
- **REUSE:** sherpa-onnx remains the alternate optional offline ASR engine; current upstream license is Apache-2.0.
- **ADAPT:** Nika keeps those engines behind `FasterWhisperTranscriber` / `SherpaOnnxWhisperTranscriber` and emits Nika `EngineDescriptor`, `ModelDescriptor`, `Segment`, and `TranscriptionResult` contracts.
- **CUSTOM (thin):** `model_evidence.py` supplies Nika-specific local artifact identity, fail-closed integrity checks, path-indirection rejection, and provenance binding. No generic ASR/RAG/framework layer is added.

No dependency is added to the base package by this batch. Heavy ASR engines/models remain optional and externally acquired through an explicit product/user-controlled action.

## Engine, runtime/binary, and model license separation

Do not infer one license from another.

1. **Engine package:** Faster-Whisper records MIT in `EngineDescriptor`; sherpa-onnx records Apache-2.0.
2. **Native/runtime dependency:** Faster-Whisper currently declares CTranslate2 as a dependency. Its license must be reviewed as its own distribution component when Nika packages that optional runtime. This batch does not bundle it and therefore does not manufacture binary/package provenance.
3. **Model:** every activated model must carry its own non-empty `ModelDescriptor.license_reference`. Nika does not infer a model license from the engine license.

A future packaged optional-component flow must record the exact installed package/binary versions and notices for the release candidate. This runbook is not that packaging proof.

## Local identity policy

`LocalModelEvidence` observes:

- total regular-file bytes;
- file count;
- optional deterministic SHA-256 bundle digest.

Observed size is always bound into the runtime `ModelDescriptor`. If the approved descriptor already declares `size_bytes`, a mismatch fails with `CHECKSUM_MISMATCH` before the ASR runtime is imported.

Payload hashing is deliberately **not automatic**. It is performed only when the approved `ModelDescriptor` already supplies `sha256`. This prevents multi-gigabyte model files from being read merely to start an optional adapter. When a checksum is supplied, the local bundle must match before the native ASR runtime is imported.

The bundle digest schema is `nika-media-local-model-v1`:

- Faster-Whisper directory: sorted UTF-8/POSIX relative file names + file sizes + file bytes;
- sherpa-onnx: sorted semantic roles (`decoder`, `encoder`, `tokens`) + file sizes + file bytes.

This is a Nika local-bundle digest. An upstream checksum for one individual model file must not be copied into `ModelDescriptor.sha256` unless it is actually the digest of the complete Nika bundle under this schema.

## Filesystem safety

Model paths may contain spaces and Unicode. Runtime APIs receive resolved paths as arguments, never shell text.

The evidence boundary rejects symbolic links and Windows junctions for the selected model root/files, every lexical ancestor of those paths, and nested directory entries. That prevents a nominal model path such as `parent-link/model` from causing integrity inspection to traverse unrelated credential/profile/private-data locations.

## Failure semantics

The model preflight happens before optional runtime import/construction.

- missing local directory/file → `COMPONENT_MISSING`;
- empty local model directory → `COMPONENT_MISSING`;
- symlink/junction indirection, including ancestor indirection → `PATH_ESCAPE`;
- approved size/checksum mismatch → `CHECKSUM_MISMATCH`, non-retryable;
- blank model license reference → validation failure;
- engine package missing → existing `COMPONENT_MISSING` behavior;
- Faster-Whisper runtime construction always receives `local_files_only=True`.

No failure path downloads a model or falls through to a remote identifier.

## Evidence tests

`tests/test_media_asr_model_evidence.py` covers:

- Unicode/space local model paths;
- local-only Faster-Whisper construction;
- exact observed model size in provenance;
- engine/model license separation;
- checksum success and mismatch before runtime import;
- size mismatch fail-closed behavior;
- no payload read when checksum verification was not explicitly requested;
- portable selected-entry and parent-path indirection rejection;
- explicit empty model directory failure;
- sherpa encoder/decoder/tokens role-bound identity.

Existing media tests remain authoritative for chunk restart, silence/empty-audio suppression, resource admission, timestamps, and Corpus handoff. This batch does not weaken those gates.

## Human/accessibility truth

This backend batch has no user-facing control changes.

`HUMAN_TESTED=false` and `NVDA_VERIFIED=false` remain unchanged. Automated tests may not change either value.
