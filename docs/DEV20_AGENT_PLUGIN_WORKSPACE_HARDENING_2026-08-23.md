# DEV20 Agent Builder / Plugin & Workspace SDK hardening — 2026-08-23

Status: MANUAL-DEV20 implementation candidate. This is a hardening addendum to the integrated
`docs/M6_AGENT_BUILDER.md` and `docs/M9_PLUGIN_WORKSPACE_SDK.md`; it does not replace either
canonical subsystem.

## Compatibility decision

The first DEV20 candidate incorrectly introduced a second `nika_core.workspace_sdk` schema and
discovery stack. That architecture is rejected. The current candidate adapts the already-integrated
`nika_core.builder`, `nika_core.plugins`, `nika_core.workspaces`, and M1
`kernel.workspace_plugin` contracts.

The public SDK no longer returns `importlib.metadata.EntryPoint`. Python packaging entry points
remain the reused discovery mechanism internally, while Nika exposes a stable
`EntrypointDescriptor`. Discovery reads metadata only. Loading a selected descriptor is a separate
operation, and adapter construction remains a later explicit activation step.

## REUSE -> ADAPT -> CUSTOM(thin)

- REUSE Pydantic v2 strict/frozen schemas and JSON-schema-capable models.
- REUSE Python `importlib.metadata.entry_points()` internally for installed-package discovery.
- REUSE existing ToolSpec/ToolRisk, Action Registry IDs, SQLiteStore and AuditLog.
- ADAPT integrated M6 Agent Builder with permission-scope compilation, deterministic proposal
  compilation, activation-time live-catalog validation and durable metadata integrity checks.
- ADAPT integrated M9 Plugin/Workspace SDK with provider-neutral entry-point descriptors,
  permission/action declarations, explicit plugin upgrade and durable workspace activation.
- CUSTOM(thin) only Nika activation-subject/authority ports, privilege attenuation, immutable
  activation generations and fail-closed restart policy.
- MCP remains the existing external tool interoperability boundary.

No new third-party dependency, ORM, database, generic plugin manager, discovery framework, approval
signer or sandbox is introduced.

## Agent Builder authority boundary

Natural-language generation is proposal-only:

`request -> AgentDraftService -> AgentDefinition -> AgentCompiler -> CompilationResult`

`AgentCompiler` resolves every tool and every non-empty tool scope against host catalogs. Unknown
tool IDs, model/schedule/resource references, permission scopes or risk mismatches fail closed.

High-impact AgentDefinition activation does not accept caller-supplied tool IDs as approval truth.
A host-injected `ActivationAuthorityPort` verifies opaque approval references against an exact
Nika-owned activation subject. Without a trusted verifier, R4 activation fails closed.

The repository re-derives the highest risk and required R4 tool IDs from the immutable
AgentDefinition immediately before activation. It performs the same derivation on later
`get()`/`active()` reads. Therefore persisted `highest_risk` or `required_approvals_json` corruption
cannot become plausible restart authority after an earlier legitimate activation. Numeric durable
identity is also strict: Boolean/REAL/string coercions are not accepted as integer authority.

An older AgentDefinition version cannot replace a newer active version, including after restart.
Agent configuration approval remains distinct from execution-time R4 approval. Activating a
definition only exposes an approved configuration; each later high-impact effect remains governed
by the normal M10/ToolExecutor boundary.

## Plugin permission and discovery boundary

`PluginManifest` declares stable dotted permission IDs and Action Registry IDs in addition to
capability/risk declarations. `PluginPolicyCatalog` validates those IDs before registration.
Unknown IDs fail closed.

Plugin permissions are a declaration ceiling, not an automatic grant. Activation requires an
explicit permission subset whenever the manifest declares permissions. The selected set must be a
subset of `PluginManifest.permission_ids`, is bound into the exact activation subject, and becomes
the runtime's effective permission set only after trusted verification.

A plugin already active with one permission set cannot be silently reactivated with a wider or
different set. The caller must deactivate it and perform a new explicit authorization. Extra
approval references cannot manufacture undeclared permissions.

Plugin upgrade is explicit compare-and-swap on the previously registered manifest version and
requires the plugin to be inactive. Version strings remain package-owned opaque identifiers; Nika
does not fabricate semantic-version ordering.

`inspect_plugin_entrypoints()` isolates arbitrary third-party import/registration failures and does
not expose exception text. Duplicate discovered plugin identities are quarantined instead of
selecting the first package.

Importing a selected installed Python entry point executes package code. Manifest permissions are
authorization metadata, not a Python sandbox. Untrusted code still belongs behind the existing
Toolsmith/CodingWorker containment and approval boundaries.

## Workspace compatibility, durability and attenuation

The canonical M9 `WorkspaceManifest` remains authoritative. Each `PluginRequirement` may request
only permissions/actions declared by the exact required plugin manifest. Capability grants retain
their explicit risk ceilings.

`WorkspaceActivationRepository` stores canonical workspace/plugin manifest metadata and the
workspace's selected effective permission IDs. It never imports or constructs plugin adapters when
reconstructed after restart.

The extension schema is initialized under SQLite `BEGIN IMMEDIATE`, serializing first-writer schema
creation without consuming the shared core migration number. Future unsupported extension-schema
versions fail closed.

Both activation and restart reads validate durable evidence against the immutable workspace
manifest. `effective_permissions_json` is evidence, not authority: the repository recomputes the
selected permission set through `WorkspaceCatalog` and rejects any widened, narrowed or otherwise
changed persisted value. The persisted reviewed plugin list must exactly match the plugin IDs and
order declared by `WorkspaceManifest.required_plugins`; removing, adding or substituting reviewed
plugin metadata fails closed. The stored manifest-version column must also equal the decoded
WorkspaceManifest version.

Before activation, the current host plugin manifests are compared with the exact reviewed plugin
objects, so version/content drift after review cannot silently activate. Durable activation
`generation` and extension-schema versions use strict integer identity rather than coercion.

Manifest `version` remains the workspace-owned opaque version. A separate monotonic integer
activation `generation` is assigned by the repository. Reusing one manifest version with different
content is rejected. A previously active newer generation cannot be replaced by an older candidate
after restart.

Therefore the AUD03 #198 family

`v1 candidate -> v2 candidate -> activate v2 -> restart -> replay v1`

fails closed and leaves v2 active.

## M10 dependency and security limits

DEV20 deliberately does not implement an approval signer, secret key, human-consent UI or parallel
M10 authority service. `ActivationAuthorityPort` is a consumer-side port for a trusted composition
root to adapt to the canonical M10 verifier/evidence mechanism after that authority is integrated.

Opaque approval references are never accepted as truth by themselves. A caller that can construct
strings, manifests, candidate JSON or fingerprints cannot authorize an authority-requiring subject
without the trusted verifier.

These contracts provide authorization and compatibility boundaries, not hostile-Python
containment. Installed plugin code remains a code-trust decision.

## Qualification matrix

Focused deterministic regressions cover:

- unknown NL-drafted tool and Agent Builder permission-scope rejection;
- R4 AgentDefinition activation without trusted authority;
- removal of the raw caller-provided tool-ID approval bypass;
- AgentDefinition permission-catalog drift before activation;
- stale AgentDefinition activation after a newer active version;
- persisted Agent Builder risk/R4 metadata downgrade before activation and after restart;
- provider-neutral metadata-only plugin/workspace discovery;
- isolated invalid plugin registration and duplicate plugin identity;
- plugin permission/action catalog validation;
- explicit plugin permission subset attenuation;
- active-plugin permission widening rejection;
- explicit plugin upgrade compare-and-swap;
- workspace permission/action/plugin-declaration containment;
- exact current plugin-manifest drift before workspace activation;
- persisted workspace effective-permission widening before activation and after restart;
- persisted workspace reviewed-plugin-set corruption;
- stored workspace manifest-version identity mismatch;
- strict durable activation-generation identity;
- SQLite restart preservation without adapter construction;
- stale workspace generation rollback rejection;
- future workspace-activation schema rejection.

Authoring preflight requires Python compile, repository line-length policy, focused semantic checks,
then exact PR-head Core CI on Ubuntu/Windows and complete M12. Only the final exact head receives
acceptance evidence.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
`INTEGRATED=false`
