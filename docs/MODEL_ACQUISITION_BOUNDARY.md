# Nika Core — optional model acquisition boundary

Updated: 2026-08-19.

## Purpose

Model inference and model acquisition are different product actions. A normal `ModelRequest` may select a model that is already available to a provider, but it never grants permission to download model files, execution-provider payloads or other optional heavy components.

This boundary exists for privacy, bandwidth/resource control, licensing and reproducible release evidence.

## Binding rules

1. Ordinary inference is network-download-free with respect to optional model acquisition. If an embedded model is not cached, inference fails closed with a typed unavailable error.
2. Acquiring a model requires a separate `ModelDownloadAuthorization` bound to the exact provider ID and exact model alias. Release/physical-proof paths additionally pin the provider's exact public model/variant identity with `expected_model_id`.
3. The authorization carries a non-empty `license_reference`. This is model-license evidence supplied/reviewed by the product/release layer; it is not inferred from the SDK/engine license.
4. Changing `ModelRequest.model` cannot grant download permission.
5. Provider-wide `allow_download=True` behavior is rejected for Foundry Local. Callers must use the explicit model-management action.
6. Foundry Local model downloads use the SDK's documented download cancellation event. Cancellation of the Nika coroutine or expiry of the explicit download timeout signals that event.
7. Foundry model lifecycle work and in-process inference share the same provider slot. A cancelled/timed-out download retains that slot until the native download worker exits, so inference is not started on top of unfinished model acquisition.
8. Foundry inference may be protected by a Nika-owned `ModelResourcePolicy` using the existing `ResourceObserverPort`: CPU, system-memory percentage and minimum available-memory preflight limits fail closed before native model execution starts.
9. Large model files remain outside the mandatory base package. Packaging/distribution ownership remains with release engineering; this document defines only the intelligence-side authorization boundary.
10. Physical-Windows inference proof remains distinct from adapter/unit/SDK-import evidence.
11. Foundry's manager is process-wide. A `FoundryLocalProvider` unloads only models that the same provider instance loaded. It must not unload a model that was already loaded by another consumer, and it refuses to race `close()` against native work that still owns the provider slot.

## Foundry Local adaptation

Microsoft's current Python SDK exposes model discovery, public model/variant `id`, alias/capability/cache state, explicit `model.download()`, load/unload and inference separately. It also documents a `threading.Event` cancellation mechanism for model and execution-provider downloads. Nika adapts those primitives rather than inventing its own downloader.

`FoundryLocalProvider.complete()` therefore never calls `model.download()`.

`FoundryLocalProvider.download_model()` accepts `ModelDownloadAuthorization`, performs only the exact authorized catalog model acquisition, validates an optional exact variant-ID pin before any download, applies an explicit total download timeout, passes through the SDK cancellation event and returns read-only cache/model evidence.

The public Python model surface does not provide a dependable model-license field that Nika should treat as authoritative. Nika therefore records the exact public model ID separately from a human-reviewed `license_reference`; it does not inspect private SDK internals to manufacture license/version truth. If a numeric version suffix is present in the public ID, it may be exposed as convenience metadata, while the full exact ID remains the authoritative provider artifact identity.

## Evidence and licensing

The Foundry Local SDK package license and the selected model license are separate facts. Every physical/release proof records the SDK package/version, exact public model ID and a separately reviewed model-license reference. Optional cache-tree hashing adds deterministic artifact evidence after acquisition and also records file count and total bytes.

`scripts/prove_foundry_local.py` is the controlled physical-Windows collector. It requires the Windows WinML SDK package, exact `--model`, exact `--model-id`, operator-supplied `--model-license`, and runs inference through the real `ModelGateway`. It records platform/resource evidence, performs an unload/reload inference cycle and does not write raw prompt/response text to the evidence file. `--allow-download` remains an explicit opt-in action; `--hash-model-cache` remains separately opt-in because hashing large model trees is expensive.

## Compatibility

The old constructor switch `allow_download=True` is deliberately rejected fail-closed. `allow_download=False` remains accepted for compatibility. Callers that genuinely intend to acquire a model must migrate to the explicit `download_model()` action.

`expected_model_id` is optional for ordinary installed-model operation so existing callers remain compatible, but it is required by the physical/release evidence collector so an alias changing to another provider variant cannot silently receive release identity credit.

## Acceptance tests

The focused regression family must prove at minimum:

- uncached inference does not download;
- a request-level model override does not download;
- the legacy provider-wide download switch is rejected;
- missing model-license evidence cannot create an authorization;
- a wrong-provider authorization is rejected;
- an exact model-ID mismatch is rejected before download or inference;
- the exact explicit download action can cache the model and then inference can proceed;
- download timeout/cancellation signals the SDK event and retains the provider slot until the native worker exits;
- configured model resource limits block before model load/inference;
- provider shutdown unloads provider-owned models but never a preloaded model owned by another consumer;
- provider shutdown fails closed while timed-out native work is still active;
- missing catalog models remain typed unavailable errors;
- successor AUTO02 PRs run the Windows Foundry SDK import proof without downloading a large model.
