# M9/M10 Toolsmith security bridge

Updated: 2026-08-19.

Status: implementation candidate. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.

## Why this bridge exists

M9 originally introduced a small Software Factory workspace API before the durable Toolsmith
domain existed. DEV02 later integrated the canonical `nika_core.toolsmith` contracts, including
`CodingJob`, permission ceilings, strict repository-relative paths, typed process/network policy,
resource limits, recovery state and explicit isolation classes.

The old M9 API remains temporarily available for backward compatibility, but new downstream
execution must not create a second competing Software Factory state or permission universe.

This bridge therefore consumes the canonical Toolsmith `CodingJob` and adapts only its downstream
enforcement surface into M10 `SecurityPolicy`.

## Security invariants

The bridge is fail-closed:

- a downstream tool binding is accepted only when its permission already exists in the Toolsmith
  job permission ceiling;
- duplicate permission or tool bindings are rejected;
- every action is capped by the risk ceiling declared on its binding;
- `EXTERNAL_SIDE_EFFECT` and `HIGH_IMPACT` intents are marked approval-required even if the caller
  forgets to request approval;
- writes pass the canonical Toolsmith `AllowedPathPolicy` before M10 path resolution, preserving
  `.git`, ADS/colon, absolute-path, drive-path and parent-traversal rejection;
- Toolsmith network DENY remains DENY; approved hosts are copied exactly rather than widened;
- allowed executables are copied from the typed Toolsmith `ProcessPolicy`;
- M10 write/network/process budgets are supplied explicitly instead of being guessed from
  Toolsmith's different resource-budget dimensions.

## Sandbox truth

`POLICY_ONLY` is not an operating-system sandbox.

`PROCESS_CONTAINED` is also insufficient to claim that untrusted candidate code cannot touch the
filesystem or network outside policy. Process-tree containment and filesystem/network sandboxing
are different security properties.

The bridge therefore sets `untrusted_execution_ready=true` only for Toolsmith leases already
classified `OS_SANDBOXED` or `REMOTE_SANDBOXED`. If a caller requests untrusted candidate
execution on a weaker lease, bridge construction fails before an action can be formed.

This does not manufacture an OS sandbox. It only prevents a weaker upstream claim from being
upgraded by the M9/M10 adapter.

## Contract ownership

REUSE:

- canonical `nika_core.toolsmith.CodingJob`, `AllowedPathPolicy`, `NetworkPolicy`,
  `ProcessPolicy` and `IsolationClass`;
- existing M10 `SandboxPolicy`, `SecurityPolicy`, `ExecutionBudget`, `ActionIntent` and approval
  ledger.

ADAPT:

- M9 workspace capability names are explicitly bound to downstream tool IDs by
  `CapabilityToolBinding`;
- Toolsmith allowed paths, network hosts and executables are translated into the existing M10
  policy shape without changing either source contract.

CUSTOM thin:

- permission-to-tool attenuation;
- risk-ceiling checks;
- explicit downstream budget input;
- truthful isolation classification at the cross-layer boundary.

No OpenHands, Codex, shell runtime, model dependency or new sandbox dependency is introduced by
this package.

## Backward compatibility

`nika_core.workspaces.software_factory` is intentionally not deleted in this batch because the
integrated M9 public API and tests still use it. New durable Toolsmith work should import
`nika_core.toolsmith` contracts and use `build_toolsmith_security_envelope` for the M10 boundary.

A later removal of the legacy M9 request/result/worker types requires an explicit deprecation and
migration PR; silently aliasing the incompatible old and new types would be unsafe.

## Acceptance evidence for this batch

Focused tests must prove:

1. canonical Toolsmith paths remain stricter than the generic M10 resolver;
2. no binding can grant a permission absent from the original job ceiling;
3. a low-risk binding cannot be relabelled as a high-impact action;
4. POLICY_ONLY and PROCESS_CONTAINED cannot claim readiness for untrusted candidate execution;
5. OS_SANDBOXED may cross the bridge without weakening its declared isolation;
6. network allowlists and M10 budgets remain enforced;
7. external side effects and high-impact operations still require explicit M10 approval.

The exact PR head must then pass the normal Ubuntu and Windows Core CI before integration credit.
A Windows package or M12 candidate may be prepared only after exact-head source validation is
green. Human/NVDA status remains false until real human testing of the same packaged candidate.
