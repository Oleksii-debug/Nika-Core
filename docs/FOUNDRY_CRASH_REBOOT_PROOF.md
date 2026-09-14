# Physical Foundry crash/reboot proof

This proof is a successor to the normal Foundry lifecycle collector in
`scripts/prove_foundry_local.py`. It exercises the #804 durable in-flight marker through the
real Nika runtime path and requires an actual Windows reboot between its two phases. Hosted CI
can validate the harness, but cannot satisfy this physical gate.

## Preconditions

- Run on the target Windows machine with Nika's `agent` and `embedded-ai` optional components.
- Use the exact reviewed Foundry model alias, public variant ID, and license evidence reference.
- Use a new, dedicated `--proof-dir` on storage that survives reboot. Never reuse a directory
  from a prior run.
- If the model is not cached, add `--allow-download`; this performs the existing explicit model
  management action before inference. Add `--hash-model-cache` when the release evidence needs
  a cache tree checksum.
- The selected model must not already be loaded by another Foundry consumer.

## Phase 1: ARM and hard process loss

```powershell
python scripts/prove_foundry_crash_recovery.py --arm `
  --proof-dir .\artifacts\foundry-crash-proof-01 `
  --model <exact-alias> `
  --model-id <exact-public-variant-id> `
  --model-license <reviewed-license-reference> `
  --hash-model-cache
```

ARM starts a child Nika process, runs a real Foundry request through
`ModelGatewayAgentRuntime -> TaskRuntimeCoordinator -> ModelGateway -> FoundryLocalProvider`,
waits for the durable ACTIVE model-gateway marker and an in-flight loaded-model observation,
suspends the child, rechecks the exact durable session while it is frozen, then hard-terminates
that child. The controller shell remains alive and writes `crash-arm-evidence.json`.

A successful ARM phase is **not** the final proof. Reboot Windows before VERIFY. Merely closing
and reopening a shell is insufficient.

## Phase 2: VERIFY after reboot

```powershell
python scripts/prove_foundry_crash_recovery.py --verify `
  --proof-dir .\artifacts\foundry-crash-proof-01 `
  --output .\artifacts\foundry-crash-reboot-evidence.json
```

VERIFY fails closed unless the OS boot timestamp advanced. It then requires the same Foundry SDK
package/version and exact model identity, reopens the same Nika proof database, runs canonical
startup recovery, and requires the crash-left opaque inference to become
`CHECKPOINT_UNAVAILABLE` with no replay and no false COMPLETED task. Finally it runs a fresh,
explicitly requested Foundry inference and proves provider-owned unload.

The final JSON stores model/resource/recovery facts plus hashes and lengths for model output; it
does not store the raw prompt, raw response text, or absolute Foundry cache path. Set
`PHYSICAL_WINDOWS_FOUNDRY_CRASH_PROVEN=true` only when the final VERIFY command succeeds on the
physical target and its evidence is retained with the exact candidate/release record.
