# Real Foundry Local acceptance

This is the physical Windows acceptance path for the existing Nika Foundry Local adapter. It does not replace `scripts/prove_foundry_local.py`; it composes that canonical proof twice in fresh Python processes and binds the result to one clean Git commit.

## What it proves

A successful `scripts/prove_foundry_acceptance.py` run records and validates:

- the exact clean Nika `HEAD` SHA and hashes of the acceptance wrapper and canonical child proof;
- child Python import authority is pinned to this exact checkout: safe-path mode (`-P`), `PYTHONPATH=<checkout>/src`, and user-site packages disabled, so another installed Nika package cannot silently supply the code under test;
- exact Foundry provider, model alias, public model ID and operator-supplied reviewed license reference;
- Windows/platform, Foundry WinML SDK and resource snapshots already emitted by the child proof;
- a fixed temperature-zero prompt fixture whose response must be exactly `NIKA_FOUNDRY_LOCAL_OK`;
- real ModelGateway response metadata, latency and usage from both child runs;
- cached/loaded lifecycle evidence, including unload/reload/final unload inside each child;
- two independent child-process executions on the same SHA/model/fixture as restart/re-run evidence;
- no download authority: the wrapper has no `--allow-download` option and fails unless the child evidence says no explicit download ran;
- no fallback: every accepted response must remain `provider_id=foundry-local`, `provider_kind=local`, and the exact requested model alias.

The final JSON deliberately does not include the machine-specific model cache path. The canonical child proof reports only whether such a path was available unless the operator explicitly asks for a cache digest.

## Prerequisites

Use a physical Windows machine with the repository checked out at the exact commit you want to qualify. Install the project's `agent` and `embedded-ai` optional components so resource evidence and `foundry-local-sdk-winml` are present. The selected model must already be cached. Acquisition is a separate explicit action and is outside this acceptance harness.

The operator must supply the exact Foundry alias, exact public selected model ID, and a model-license identifier/evidence reference that was actually reviewed. The harness never infers or invents model-license metadata.

## Run

From a clean repository checkout in PowerShell:

```powershell
python scripts/prove_foundry_acceptance.py `
  --model '<EXACT_FOUNDRY_ALIAS>' `
  --model-id '<EXACT_PUBLIC_MODEL_ID>' `
  --model-license '<REVIEWED_LICENSE_REFERENCE>' `
  --output '.\foundry-local-acceptance-evidence.json'
```

Optional resource policy flags from the canonical proof may be supplied: `--max-cpu-percent`, `--max-memory-percent`, and `--min-available-memory-gb`. `--hash-model-cache` may also be used when exact cache-byte evidence is required; it can be expensive for large artifacts.

Do not add `--allow-download`: the acceptance wrapper intentionally exposes no such flag. If the model is absent, stop and perform any approved acquisition as a separate explicit model-management action, then rerun acceptance from the same intended Nika commit.

## Acceptance interpretation

Only a successfully written aggregate JSON with `physical_windows_foundry_inference_proven=true`, `retest_runtime_required=false`, two validated child runs, and the intended exact `nika_sha` is physical Foundry evidence. Hosted/cloud CI that lacks the real Windows Foundry runtime/model must not set those fields by simulation.

This harness proves fresh-process rerun, not a Windows OS reboot. If the release gate requires reboot persistence, perform one additional physical step: reboot Windows without changing the repository SHA or model cache, rerun the same command, and retain both aggregate JSON files. The two files must name the same Nika SHA, provider/model identity and reviewed license reference, and both must independently pass the fixed response/no-download/no-fallback contract.

Until that real machine run exists for the exact release candidate, classify Foundry physical acceptance as `RETEST_RUNTIME_REQUIRED` rather than PASS.
