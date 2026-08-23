# Agent Builder and Workspace SDK security contract

Status: implementation contract for M6 Agent Builder and M9 Plugin/Workspace SDK.

## Reuse decision

Nika reuses Pydantic v2 and its JSON Schema export for declarative documents, Python
`importlib.metadata` entry points for installed-workspace discovery, and the existing MCP boundary
for external tool interoperability. Nika adds only thin policy adapters for stable IDs, R0-R4
compilation, compatibility, approval ceilings, immutable version activation, and persistence. This
is intentionally not a second schema engine or a second discovery framework.

## Agent definition flow

`AgentDraftService` may ask a configured Model Gateway to produce an `AgentDefinition` JSON draft.
The model output is never permission truth. `AgentProposalService` immediately passes the parsed
Pydantic object through `AgentCompiler`, which validates the current model/schedule/resource
references, registered tool ID, exact tool risk, and every requested tool permission scope.
Unknown tools, references, or permission scopes fail closed.

A non-empty permission scope is valid only when the current compiler permission catalog explicitly
lists it for that exact tool ID. Absence from the catalog is denial, not a wildcard. R4 tools remain
explicit human-approval requirements; the builder does not manufacture approval identity or bypass
the M10 trusted approval boundary.

`AgentDefinitionRepository` keeps immutable integer versions. `AgentActivationService` recompiles
the exact persisted definition against the live catalogs immediately before activation. Tool/risk
or permission-catalog drift therefore blocks activation and requires a newly reviewed version.

## Workspace manifest

`WorkspaceManifest` is provider-neutral Pydantic data with:

- immutable `workspace_id` and monotonically increasing `version`;
- manifest `format_version` and Nika `sdk_api_version`;
- stable capability IDs;
- stable permission IDs;
- stable dotted Action Registry IDs.

`WorkspaceValidationCatalog` is constructed by the host from authoritative Nika registries. A
manifest with an unsupported SDK API version or any unknown capability, permission, or action ID is
invalid. Framework, model-provider, and `importlib.metadata.EntryPoint` types are not public SDK
contracts.

## Discovery is not activation

The `nika_core.workspaces` Python entry-point group remains the sole installed-workspace discovery
mechanism. `discover_workspace_entrypoints()` returns Nika-owned descriptors containing only
strings and does not load plugin code. `load_workspace_plugins()` is a separate operation: each
entry point is loaded and validated independently, so one broken or incompatible plugin is isolated
instead of preventing discovery of valid plugins. Duplicate `(workspace_id, version)` providers are
ambiguous and all duplicates are rejected rather than selecting one by import order.

Python plugin installation is a code-trust boundary: importing an installed Python distribution can
execute that distribution's module code. The manifest permission system is an authorization boundary
for Nika capabilities; it is not a Python sandbox and must not be represented as one. Untrusted code
requires the separate worker/sandbox controls owned by the Software Factory/security lanes.

## Permission compilation and activation

`compile_workspace_activation()` first validates compatibility and all stable IDs, then requires the
approved permission ceiling to contain every permission declared by the manifest. Effective plugin
permissions are exactly the manifest permissions. Extra permissions in an approval set are ignored;
a plugin cannot acquire capabilities that it did not declare.

`WorkspacePluginRepository` persists immutable candidate versions, entry-point identity, and the exact
effective permission set. Activation is atomic: the previous active version becomes retired only as
the reviewed candidate becomes active. `WorkspaceActivationService` recompiles compatibility and the
current approval ceiling immediately before that transition. Restarting Nika reconstructs the exact
active manifest and effective permissions from SQLite.

## Failure semantics

The boundary fails closed on malformed manifests, unsupported SDK versions, unknown IDs, missing
approvals, definition mutation, permission drift, ambiguous duplicate plugin versions, skipped
version numbers, and missing activation candidates. Plugin import/validation exceptions are reported
per entry point as Nika-owned failure records; provider exception objects are not part of the public
contract.

## Verification boundary

Automated tests cover model-draft-to-registry validation, unknown permission denial, R4 activation,
live-catalog drift, manifest compatibility, Action Registry IDs, permission-ceiling non-expansion,
invalid plugin isolation, duplicate identity/version rejection, monotonic upgrades, and SQLite restart
persistence. These tests do not confer `HUMAN_TESTED` or `NVDA_VERIFIED`.
