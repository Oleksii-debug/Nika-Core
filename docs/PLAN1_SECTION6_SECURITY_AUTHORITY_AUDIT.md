# Plan 1 Section 6: verified generic Core security authority

Independent 6А/6Б/6В and aggregate Section6 are DONE.

| Boundary | Existing authority and verified behavior |
| --- | --- |
| Standing permissions | Canonical store, exact authority carriers, detached bindings, schema/storage validation, bounded grant/delegation and ancestor revocation |
| Per-action approval | Existing HMAC issuer/verifier, exact action fingerprint, expiry and one-shot consumption; locked approval/budget tail preserved |
| Executable scopes | Bare names require exact name/case; path grants require matching kind/identity, Windows absolute paths retain case-insensitive comparison; traversal rejected |
| Tool authority | Bounded NFC/strict UTF8 arguments and schemas; detached registry/catalog/policy/guard snapshots; approved arguments retained across callbacks |
| Durable effects | Existing ledger immutable receipts preserved; completion/uncertainty require the exact pending task/type/fingerprint/generation; corrupt replay cannot become success |
| Credentials and identity | Existing broker/protected store, scoped/identity snapshots, exact built-in integer lifetime/generation, live material and issue/expiry checks, rotation/revocation |
| MCP routing | Immutable server/risk/target snapshot, exact route segments, bounded detached arguments and cursor pagination; canonical ToolExecutor remains sole effect/approval gate |

Canonical #846/#437/#1233/#1237/#880 were reused. Static review found no lost
current-main guards or functions; existing code was adapted rather than replaced.
Completed old-head CI failures proved obsolete test expectations in executable
scope and snapshot admission. Same-lineage repairs preserve explicit rejection,
handler nonexecution, empty-ledger/no-handle and unchanged-audit assertions.

Qualified combined candidate2eb75950111e5f5b82079569a8da3958b47774b4 passed
Core44215159104 and fullM1244215159139. Hosted Ubuntu3715passed/5skipped;
Windows3713passed/7skipped; dependency/Ruff/compile passed. Independent exact
source reviews found no remaining scoped blocker. No local pytest result is
invented during execution-service unavailability.

Exact credential component b3fd7c2366450120bc81167618e7a5629d63f09f separately
passed PF344214543259: both cffi and ctypes actual hosted Windows Credential
Manager regressions, protected-store lifecycle, packaged adapter and cleanup.
Its three blobs are identical in the final composition. Final-head skipped PF3
does not receive PASS by inheritance; this is source-bound component evidence.

Integrations:
- #846 main1c1d5fc8ecdb472a9a016e05fbe32a82485e4d8f.
- #1237 squash4f0cf5b289c6e6fc3e9cd8129aea21294ffb7f91.
- #437 main8e8da6a08ebb95fa937029b3fe945abbbbc9c066.
- #1233 squasha1cca8fee132cae0f219d38c6724648ec8bc1904.
- #880 squasha6526deafdec46930ac0affa1adddb22864b525d.

Every merge read back its expected scoped tree; final
2d10140d2eb48814d3eb4017ed6a3ded5462c0a4 exactly equals the complete qualified
candidate tree. Squash integrations have only the observed main parent; unrelated
old terminal/Memory/scheduler ancestry was not promoted.

M12 packaged job134509702268 verified notices, manifest, actual hosted UIA,
keyboard/autostart and the exact extracted final distributable. Artifact14388887166
digest2b2521ddcd08e193b515a3dbd045deca3982274010675d465d1f49deb4ed25ad;
distributable ZIP SHA256:
81f847ec4e92674110d605fcbf00a05a80c10c44202e07f2863f54800f6c5e92.

Scope limitations remain explicit: CapabilityBroker/plugins Section9 and known
CLOUD ModelGateway authorization/revocation Section10/Plan3 remain open. These
Core results do not imply whole-platform security or physical owner acceptance.
HUMAN_TESTED=false; NVDA_VERIFIED=false; PRODUCTION_RELEASE_READY=false.
Windows login execution and human NVDA remain unverified; optional skipped jobs
and main-only provenance do not become PASS.

No real user credentials, production databases, private logs, browser profiles or
model weights were committed. Windows credential proofs use synthetic scoped
test material on ephemeral runners. Existing Git history supports rollback.
Drive writes remain denied403; GitHub records terminal status.

Next actionable is Plan1 7А. Existing #1107 typed scheduler snapshot and resource
durability lineages are being qualified; Section7 is NOT DONE.
