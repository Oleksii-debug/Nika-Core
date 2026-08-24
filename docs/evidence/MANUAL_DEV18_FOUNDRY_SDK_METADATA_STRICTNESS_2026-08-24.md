# MANUAL-DEV18 — Foundry SDK metadata strictness

Date: 2026-08-24
Parent production candidate: PR #272 `f9170f028142e077307effcb0bbfc3e6de17e20f`
Live main at branch creation: `23c7c1ce97b263b4aafa61bdcbace207b4476a3d`

## Finding

The official Foundry Local public `IModel` surface declares `is_cached` and `is_loaded` as booleans, `id` and `alias` as strings, and `context_length` as `int | None`. The current Nika adapter still uses Python truthiness/string/integer coercion for several of those fields.

A malformed SDK-shaped value such as `is_cached="false"` is truthy in Python. Treating it as cached can bypass the explicit model-download boundary during inference or publish cached download evidence without an actual download. `is_loaded="false"` can similarly skip the provider-owned load boundary. Coercing malformed identity or optional metadata can manufacture plausible release/evidence values.

## Required invariant

Provider-visible SDK metadata is untrusted at the adapter boundary and must match the documented public SDK types before it can influence execution or evidence:

- required cache/load state: actual `bool` only;
- model ID/alias: actual non-empty `str` only;
- optional integer metadata: `None` or actual non-boolean non-negative `int`;
- optional string metadata: `None` or actual `str`;
- optional tool capability: `None` or actual `bool`.

Malformed metadata must fail closed as a typed non-retryable provider error. It must not cause model download, model load, chat execution, unload, or positive cache/model evidence.

## Scope

The regression branch adds no dependency, workflow, model artifact, credential, permission, provider backend, or shared ModelGateway contract. It does not select, download, load, or execute a real model. Production repair belongs to the existing #272 Foundry adapter owner; this branch is an executable child oracle until that repair is incorporated.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
`PHYSICAL_WINDOWS_FOUNDRY_INFERENCE_PROVEN=false`
