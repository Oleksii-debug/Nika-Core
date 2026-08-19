# Nika Core — optional model acquisition boundary

Updated: 2026-08-19.

## Purpose

Model inference and model acquisition are different product actions. A normal `ModelRequest` may select a model that is already available to a provider, but it never grants permission to download model files, execution-provider payloads or other optional heavy components.

This boundary exists for privacy, bandwidth/resource control, licensing and reproducible release evidence.

## Binding rules

1. Ordinary inference is network-download-free with respect to optional model acquisition. If an embedded model is not cached, inference fails closed with a typed unavailable error.
2. Acquiring a model requires a separate `ModelDownloadAuthorization` bound to the exact provider ID and exact model alias.
3. The authorization carries a non-empty `license_reference`. This is model-license evidence supplied/reviewed by the product/release layer; it is not inferred from the SDK/engine license.
4. Changing `ModelRequest.model` cannot grant download permission.
5. Provider-wide `allow_download=True` behavior is rejected for Foundry Local. Callers must use the explicit model-management action.
6. Foundry Local model downloads use the SDK's documented download cancellation event. Cancellation of the Nika coroutine signals that event.
7. Foundry model lifecycle work and in-process inference share the same provider slot. A cancelled download retains that slot until the native download worker exits, so inference is not started on top of unfinished model acquisition.
8. Large model files remain outside the mandatory base package. Packaging/distribution ownership remains with release engineering; this document defines only the intelligence-side authorization boundary.
9. Physical-Windows inference proof remains distinct from adapter/unit/SDK-import evidence.

## Foundry Local adaptation

Microsoft's current Python SDK exposes model discovery, explicit `model.download()`, load/unload and inference separately. It also documents a `threading.Event` cancellation mechanism for model and execution-provider downloads. Nika adapts those primitives rather than inventing its own downloader.

`FoundryLocalProvider.complete()` therefore never calls `model.download()`.

`FoundryLocalProvider.download_model()` accepts `ModelDownloadAuthorization`, performs only the exact authorized catalog model acquisition, passes through a cancellation event, and returns read-only cache/model evidence.

## Evidence and licensing

The Foundry Local SDK package license and the selected model license are separate facts. Every physical/release proof records the SDK package/version and a separately reviewed model-license reference. Optional cache-tree hashing may add deterministic artifact evidence after acquisition.

## Compatibility

The old constructor switch `allow_download=True` is deliberately rejected fail-closed. `allow_download=False` remains accepted for compatibility. Callers that genuinely intend to acquire a model must migrate to the explicit `download_model()` action.

## Acceptance tests

The focused regression family must prove at minimum:

- uncached inference does not download;
- a request-level model override does not download;
- the legacy provider-wide download switch is rejected;
- missing model-license evidence cannot create an authorization;
- a wrong-provider authorization is rejected;
- the exact explicit download action can cache the model and then inference can proceed;
- the SDK cancellation event is passed to the download worker;
- missing catalog models remain typed unavailable errors.
