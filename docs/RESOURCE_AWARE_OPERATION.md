# Resource-aware local operation profiles

Status: IMPLEMENTED ON LANE; acceptance credit requires exact branch-head CI, review, and integration.

This slice implements the named resource-profile policy required by Full Product Vision section 9 without creating a second resource manager or changing the active M3 durability owner.

## Ownership and compatibility boundary

This lane owns only:

- `src/nika_core/resource_profiles/**`;
- `tests/test_resource_profiles.py`;
- this document.

It does **not** edit or supersede `src/nika_core/resources/**`. PR #357 remains the owner of M3 ResourceManager durability, resource leases, process-generation recovery, scheduler/memory durability, and SQLite schema work.

It also does not own media execution, ModelGateway/provider lifecycle, UI, Product Factory, Toolsmith, permissions, credentials, workflows, or release policy.

## REUSE -> ADAPT -> CUSTOM (thin)

- **REUSE:** the existing `ResourceSnapshot`, `ResourceObserverPort`, `ResourceBudget`, ResourceManager boundary, and psutil-backed telemetry architecture.
- **ADAPT:** named profiles project CPU/memory ceilings into the existing `ResourceBudget` contract through `budget_for_profile()`.
- **CUSTOM (thin):** deterministic Nika product semantics for named profiles and heavy-workload mutual exclusion.

No new dependency, scheduler, telemetry engine, persistence layer, Windows service, or model runtime is introduced.

## Profiles

The initial conservative policy defaults are admission ceilings, not performance benchmark claims:

| Profile | CPU ceiling | Memory ceiling | Available-memory floor | Heavy work |
| --- | ---: | ---: | ---: | --- |
| `normal` | 95% | 90% | 512 MiB | one heavy workload at a time |
| `economy` | 75% | 80% | 1 GiB | one heavy workload at a time; idle-model unload may be recommended |
| `night_batch` | 90% | 88% | 1 GiB | one heavy workload at a time |
| `low_memory` | 70% | 70% | 2 GiB | blocked; general work only; idle-model unload may be recommended |

A later measured benchmark may tune these defaults through a compatibility decision. The current values deliberately make no claim about optimal throughput, battery life, thermals, or a specific machine.

## Heavy-workload rule

The policy classifies these as heavy:

- Chromium/browser automation;
- local model execution;
- transcription;
- explicit heavy-batch work.

By default a second heavy workload is denied while another heavy workload is active. This implements the product requirement to avoid simultaneous Chromium + local-model + transcription pressure without taking ownership of the underlying processes.

`ResourceProfilePolicy` is a pure decision layer. The caller supplies the current `ResourceSnapshot` and active workload classes. It returns a `ProfileDecision` with stable text reasons suitable for logs and accessible UI.

## Fail-closed behavior

Admission is denied for:

- unknown profile;
- unknown requested or active workload class;
- NaN/infinite/out-of-range CPU or memory telemetry;
- negative available-memory telemetry;
- profile CPU/memory pressure;
- available memory below the profile floor;
- a workload blocked by the selected profile;
- conflicting heavy work.

Unknown or malformed telemetry is never interpreted as spare capacity.

## Idle-model unloading boundary

Economy and low-memory decisions may return the recommendation code:

`unload_idle_local_model_if_safe`

This is **only a recommendation**. This slice does not determine that a model is idle, does not call ModelGateway, does not terminate a process, and does not bypass approval/permission boundaries. A future ModelGateway integration must independently prove idleness and authority before any unload effect.

## Windows and accessibility

The policy is OS-neutral and requires no administrator rights. It never changes a Windows power plan or battery setting. Results are plain deterministic text (`allowed`, `reason`, `recommendations`) so UI owners can expose them to NVDA without color-only or visual-only state.

`HUMAN_TESTED=false` and `NVDA_VERIFIED=false` until a real human NVDA run is recorded by the appropriate acceptance lane.

## Acceptance tests in this lane

Automated tests cover:

- normal-profile admission;
- deterministic mutual exclusion of heavy workloads;
- low-memory heavy-work blocking;
- economy CPU-pressure rejection;
- available-memory floor enforcement;
- malformed/NaN telemetry fail-closed behavior;
- unknown profile/workload fail-closed behavior;
- projection into the existing `ResourceBudget` contract;
- invalid scope/owner/concurrency rejection at the adapter boundary.

## Deferred integration, not hidden completion

This slice does not claim end-to-end section 9 completion. The remaining integration work requires explicit compatibility decisions with current owners:

1. persist/select the user's preferred profile through the canonical settings/UI owner;
2. feed actual active workload state from browser, ModelGateway, and transcription owners;
3. benchmark profile thresholds on Windows hardware and tune only with evidence;
4. connect safe idle-model unloading through ModelGateway authority;
5. extend Product Factory workers to remote nodes only through the Product Factory/runtime owners.

Those are intentionally not smuggled into this isolated lane.
