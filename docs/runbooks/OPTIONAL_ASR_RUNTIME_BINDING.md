# Optional ASR checksum-to-runtime binding

Status: MANUAL-DEV25 production repair for the AUD05 ASR model provenance TOCTOU family.

## Independent finding

AUD05 QA_ONLY PR #224 showed that an approved Faster-Whisper bundle could be hashed and then replaced with different bytes of the same size before the native runtime reopened the model path. The earlier checksum therefore authenticated a historical path state, not necessarily the bytes consumed by runtime construction. The same path-reopen family applies to explicit sherpa-onnx encoder/decoder/token files.

The independent QA oracle remains QA-owned and `DO_NOT_MERGE`. This repair does not copy or weaken that oracle.

## Repair

For a `ModelDescriptor` that contains an approved `sha256`:

1. Nika performs the existing explicit-local path and filesystem-indirection preflight.
2. Nika copies the selected model bundle into a private `TemporaryDirectory` owned by the adapter instance.
3. The deterministic `nika-media-local-model-v1` digest is calculated over the private snapshot bytes.
4. Size, checksum and model-license evidence are validated against the approved descriptor.
5. Only after that validation may the native ASR module be imported/constructed.
6. Faster-Whisper receives the private snapshot directory with `local_files_only=True`; sherpa-onnx receives the private snapshot encoder/decoder/token paths.
7. The snapshot owner remains attached to the adapter so the runtime-bound paths are retained for the adapter lifetime.

If the source changes between initial preflight and snapshot creation, the snapshot digest no longer matches the approved descriptor and construction fails closed with `CHECKSUM_MISMATCH` before native runtime import.

When no approved SHA-256 exists, Nika preserves the existing zero-copy local path behavior. A size-only descriptor is not relabeled as content-authenticated, and Nika does not silently duplicate multi-gigabyte models merely to manufacture an integrity claim the caller did not provide.

## Security boundary

The private snapshot prevents the normal path-reopen TOCTOU demonstrated by #224 from substituting source bytes after preflight. Existing symbolic-link, ancestor-junction and nested-indirection checks remain in force while source entries are collected and again immediately before copy.

This is not an OS sandbox or a claim that hostile arbitrary code already running with the same Nika process/user authority cannot discover or mutate process-owned files. Such hostile-code containment belongs behind the separate CodingWorker/OS isolation boundary. Optional ASR engines are trusted, explicitly installed components; model acquisition remains explicit and external.

No shell command, model download, credential/profile access, new dependency, model binary, or permission expansion is added.

## Resource tradeoff

Checksum-approved models require a private runtime copy and therefore temporary disk capacity approximately equal to the selected model bundle while the adapter is alive. This cost is limited to the stronger content-integrity path. Non-checksummed optional models remain zero-copy.

A later component manager may replace the temporary copy with a measured platform snapshot primitive only if it preserves the same exact-byte binding and Windows/NVDA-first product constraints.

## Owner regression coverage

`tests/test_media_asr_runtime_binding.py` covers:

- same-size Faster-Whisper source replacement after preflight fails before runtime import;
- equivalent same-size sherpa-onnx file replacement fails before runtime import;
- a legitimate hash-approved Faster-Whisper runtime receives a private path whose model bytes match the approved digest rather than the original caller-owned path.

The broader existing ASR model-evidence suite remains authoritative for checksum calculation, size binding, model/engine license separation, local-only construction, Unicode/space paths, filesystem-indirection rejection and empty-bundle failure.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
