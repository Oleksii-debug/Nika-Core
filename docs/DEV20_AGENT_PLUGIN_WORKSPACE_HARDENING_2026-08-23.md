# DEV20 Agent Builder / Plugin & Workspace SDK hardening — 2026-08-23

Status: MANUAL-DEV20 implementation candidate. This is a hardening addendum to the integrated
`docs/M6_AGENT_BUILDER.md` and `docs/M9_PLUGIN_WORKSPACE_SDK.md`; it does not replace either
canonical subsystem.

## Compatibility decision

The first DEV20 candidate incorrectly introduced a second `nika_core.workspace_sdk` schema and
discovery stack. That architecture is rejected. The corrective candidate removes that package from
the final tree and adapts the already-integrated `nika_core.builder`, `nika_core.plugins`,
`nika_core.workspaces`, and M1 `kernel.workspace_plugin` contracts.

The public SDK no longer returns `importlib.metadata.EntryPoint`. Python packaging entry points
remain the reused discovery mechanism internally, while Nika exposes a stable
`EntrypointDescriptor`. Discovery reads metadata only. Loading an explicitly selected descriptor is
a separate operation, and plugin adapter construction remains a later explicit activation step.

## REUSE -> ADAPT -> CUSTOM(thin)

- REUSE Pydantic v2 strict/frozen schemas and JSON-schema-capable models.
- REUSE Python `importlib.metadata.entry_points()` internally for installed package discovery.
- REUSE the integrated ToolSpec/ToolRisk, Action Registry identifiers, SQLiteStore and AuditLog.
- ADAPT the integrated M6 Agent Builder with permission-scope compilation, deterministic proposal
  compilation and activation-time live-catalog revalidation.
- ADAPT the integrated M9 Plugin/Workspace SDK with provider-neutral entry-point descriptors,
  permission/action declarations, explicit plugin upgrade and durable workspace activation metadata.
- CUSTOM(thin) only Nika activation-authority statements, permission ceilings, immutable activation
  generation state and stale/restart fail-closed policy.
- MCP remains the existing external tool interoperability boundary; DEV20 adds no competing
  protocol.

No new third-party dependency, model, plugin manager, ORM, database or generic discovery
framework is introduced.

## Agent Builder authority boundary

Natural-language generation is proposal-only:

`request -> AgentDraftService -> AgentDefinition -> AgentCompiler -> CompilationResult`

`AgentCompiler` resolves every tool and every non-empty tool scope against host catalogs. Unknown
tool IDs, model/schedule/resource references, permission scopes or risk mismatches fail closed.

High-impact AgentDefinition activation no longer accepts caller-supplied `approved_tool_ids` as
authority. The durable repository requires a host-injected `ActivationAuthorityPort` to verify
opaque approval references against an exact Nika-owned activation subject. Without a trusted
verifier, R4 activation fails closed. The repository also rejects activation of an older version
after a newer version is active, including after process restart.

Agent configuration approval remains distinct from execution-time R4 approval. Activating a
definition only makes a capability available to the configured agent; each later high-impact tool
effect remains subject to the normal M10/ToolExecutor security boundary.

## Plugin permission and entry-point boundary

`PluginManifest` now declares optional stable dotted permission IDs and Action Registry IDs in
addition to existing capability/risk declarations. The host `PluginPolicyCatalog` validates those
IDs before registration. Unknown IDs fail closed.

A plugin with declared permissions or HIGH_IMPACT capabilities cannot activate without a trusted
activation verifier. Effective plugin permissions are exactly the manifest-declared permissions.
Extra approval references never add undeclared permissions.

Plugin upgrade is explicit compare-and-swap on the previously registered manifest version and
requires the plugin to be inactive. No semantic-version ordering is fabricated; plugin versions
remain opaque package-owned strings.

`inspect_plugin_entrypoints()` isolates arbitrary third-party import/registration failures and does
not expose exception text. Duplicate discovered plugin identities are quarantined rather than
silently selecting the first package.

Importing a selected installed Python entry point executes package code. Manifest permissions are
authorization metadata, not a Python sandbox. Untrusted code still belongs behind the existing
Toolsmith/CodingWorker containment and approval boundaries.

## Workspace compatibility and restart boundary

The canonical M9 `WorkspaceManifest` remains authoritative and gains optional permission/action IDs.
Each `PluginRequirement` may request only a subset of permissions/actions declared by the exact
required plugin manifest. Capability grants continue to enforce explicit risk ceilings.

`WorkspaceActivationRepository` stores only canonical workspace/plugin manifest metadata and
effective permission IDs. It never loads adapter code on reconstruction. A lane-owned schema
migration ledger is kept in the same SQLite database without modifying the shared core migration
stream or `SQLiteStore`.

Manifest `version` remains the workspace-owned opaque version. A separate monotonic integer
activation `generation` is assigned by the repository. A previously used manifest version is
immutable. Activation verifies:

1. the exact persisted candidate;
2. current WorkspaceCatalog compatibility;
3. exact current plugin manifests against the reviewed candidate;
4. trusted authority for selected permissions/high-impact capability grants;
5. monotonic generation against the current active generation.

Therefore `v1 candidate -> v2 candidate -> activate v2 -> restart -> replay v1` fails closed and
leaves v2 active. This directly repairs the AUD03 stale-activation family from QA PR #198.

A future unsupported workspace-activation schema version also fails closed.

## Security limitations and M10 dependency

DEV20 deliberately does not implement an approval signer, secret key, human-consent UI or parallel
M10 authority service. `ActivationAuthorityPort` is a consumer-side port. The trusted Nika host may
adapt the canonical M10 verifier/evidence channel to it after that authority is integrated.

Opaque approval references are never accepted as truth by themselves. A caller that can construct
strings, manifests, candidates or fingerprints cannot activate an authority-requiring subject
without a verifier injected by the trusted composition root.

## Qualification

Focused deterministic regression coverage includes:

- unknown NL-drafted tool rejection;
- unknown Agent Builder permission scope rejection;
- R4 activation without trusted authority rejection;
- removal of the raw `approved_tool_ids` activation bypass;
- permission-catalog drift before activation;
- stale AgentDefinition activation after a newer active version;
- provider-neutral metadata-only plugin/workspace discovery;
- isolated invalid plugin registration and duplicate plugin identity;
- plugin permission/action catalog validation and non-widening effective permissions;
- explicit plugin upgrade compare-and-swap;
- workspace permission/action/plugin-declaration containment;
- exact plugin-manifest drift before workspace activation;
- SQLite restart preservation without adapter construction;
- stale workspace generation rollback rejection;
- future workspace-activation schema rejection.

Authoring preflight: all authored Python files compile under Python 3.13 and stay within the
repository 100-column policy. A self-contained SQLite/Pydantic semantic harness passes the critical
agent/plugin/workspace authority and restart invariants. Local Ruff is unavailable in the authoring
runtime, so no local Ruff GREEN is claimed. Exact PR-head Core CI and complete M12 are
authoritative.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
`INTEGRATED=false`
