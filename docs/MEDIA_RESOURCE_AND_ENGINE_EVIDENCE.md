# DEV05 media resource and engine evidence boundary

Starting integration base: `351a2c97b608ee7d8aece0e7a71ce3d43b9f7764`.

This batch closes two remaining DEV05 proof gaps without changing the shared ResourceManager,
ModelGateway, UI, release packaging, DEV01 corpus, DEV02 Toolsmith, DEV03 Trader, or DEV04
interaction contracts.

## Resource policy

`MediaResourceClaim` already exposes `min_available_memory_bytes` and
`mutually_exclusive_with`. The DEV05 coordinator now enforces those fields before asking the
canonical `ResourceManager` for its existing concurrency/CPU/memory-percent grant.

- A minimum-memory claim fails closed when no `ResourceObserverPort` is available; Nika does not
  assume that memory is sufficient.
- A minimum-memory claim is retryably blocked when the observed available bytes are below the
  requested floor.
- Mutual exclusion is symmetric: either the candidate claim or an active claim can declare the
  conflicting resource class.
- Heavy model claims share the machine-level `media_heavy_model/local_machine` resource scope and
  are hard-limited to one resident at a time. Any future concurrency above one requires a separate
  measured target-machine policy rather than a caller-provided integer.
- Reusing a live `claim_id` with identical policy is idempotent. Reusing it with different policy
  fails closed.
- Release removes the DEV05 active-claim record only after the canonical manager confirms release.

These checks are process-local lease coordination on top of the existing canonical manager. This
batch does not claim crash-durable GPU/model residency detection. A new process must measure the
machine again before granting new minimum-memory work.

## Engine, binary-build, model, and execution evidence are separate

`MediaProofManifest` schema v2 records four independent surfaces:

1. `EngineDescriptor`: engine identity, upstream/source license, version, and engine-provided
   build information when available.
2. `BinaryEvidence`: exact executable checksum and size plus the explicit binary supplier/source
   reference and binary-build license classification.
3. `ModelEvidence`: exact model/data checksum and size plus its own license and source reference.
4. `EngineExecutionEvidence`: engine ID, proof kind, fixture checksum and a SHA-256 fingerprint of
   the normalized execution result. Raw OCR text or private media contents are not copied into the
   manifest.

A model or execution evidence record is invalid unless its referenced engine is present in the
same manifest. Execution IDs are unique. `real_engine_execution_proven=true` is invalid unless
execution evidence exists for every declared engine. Local absolute paths are not serialized;
binary evidence records only the file name. No cookie, token, browser profile, API key, or other
credential field exists in this evidence schema.

## Opt-in physical proof helper

`scripts/prove_media_engines.py` is an explicit local proof collector. It never installs or
downloads FFmpeg, Tesseract, language data, or ASR models.

Required inputs are explicit local `ffprobe` and `tesseract` executables, output path, and audited
Tesseract binary supplier/license evidence. Optional fixtures can execute a real FFprobe probe and
a real Tesseract OCR page. Each successful fixture creates execution evidence from the fixture
checksum plus a hash of the normalized result. `real_engine_execution_proven=true` is emitted only
when **both** real fixture paths run successfully. Merely executing `--version`/audit commands is
not treated as a full real-engine execution proof.

Optional Tesseract traineddata evidence is all-or-nothing: file, model ID, version, license
reference, and source reference must be supplied together. File checksum and size are measured and
must agree with any descriptor values.

The helper deliberately emits `target_machine_measured=false`. A proof collected on CI or another
machine must not be promoted into target-laptop resource evidence.

## CI boundary

Normal DEV05 CI remains model-free and does not download large engines or data. The focused DEV05
workflow compiles, lints, and executes deterministic resource/evidence tests on Ubuntu and Windows.
Real-engine proof is an opt-in focused/physical action outside ordinary dependency installation.

`HUMAN_TESTED=false`. `NVDA_VERIFIED=false`.
