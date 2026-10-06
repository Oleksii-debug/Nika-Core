# Autonomous Product Factory — acceptance gates

Status: binding acceptance extension for `docs/AUTONOMOUS_PRODUCT_FACTORY.md`.
Updated: 2026-08-20.

The Product Factory does not receive completion credit from backend-only coding-worker tests. Evidence must prove product-level outcomes.

## PF0 — ProductProject durability gate
- Natural-language goal creates or updates a versioned durable ProductProject.
- Product goal, requirements, decisions, repositories, milestones, team ownership, artifacts and release state survive restart.
- Replaying the same idempotent project-creation request does not create duplicate projects.
- A changed scope becomes an explicit new project/spec version or recorded decision, not a silent mutation.

## PF1 — Research-to-Product gate
- Universal Research can produce a product/opportunity evidence package.
- The user/policy decision selects or rejects a product direction.
- The selected research evidence is linked into ProductProject provenance without manual copy/paste.
- Requirements and acceptance criteria are generated as structured versioned artifacts and can be independently reviewed.

## PF2 — Dynamic Team Composer gate
- Team composition is derived from project scope and risk rather than a hard-coded fixed agent count.
- Small projects may consolidate roles; large projects may fan out.
- New specialization can be added during execution without corrupting existing ownership.
- Child/worker permissions never exceed the ProductProject ceiling.

## PF3 — Repository graph gate
- A ProductProject can own one repository or a graph of several repositories/components.
- Each component has explicit ownership, build/test commands, dependencies and release identity.
- Parallel workers cannot silently modify overlapping ownership.
- Repository creation/connection is auditable and uses CredentialBroker references rather than plaintext secrets.

## PF4 — Autonomous implementation gate
Start from a ProductProject acceptance specification and prove:
- REUSE/ADAPT/CUSTOM research precedes custom infrastructure;
- workers implement in isolated branches/workspaces;
- source changes return exact commits/diffs and machine-readable test evidence;
- independent review can reject a candidate and force repair;
- failed candidates cannot silently promote themselves;
- production/main remains protected from direct worker mutation.

## PF5 — Multi-platform build/execution gate
- Execution-node selection is explicit and capability based.
- At least two distinct execution environments can return normalized build/test evidence through the same Nika contract.
- A task requiring an unavailable platform fails clearly or routes to an authorized suitable node; it does not falsify a successful build.
- Nodes receive only project-scoped credentials/paths/network authority.

## PF6 — Deployment gate
For a deployable test product:
- build/package succeeds on an exact release SHA;
- deployment goes first to an approved staging target;
- environment/config identity is recorded without exposing secrets;
- health verification runs after deployment;
- a deliberately bad candidate is rejected or rolled back;
- production promotion remains inside authorization policy.

## PF7 — Credential/identity gate
- No raw API key/password is stored in ProductProject, model prompt history, Git, report or ordinary logs.
- Workers receive opaque/scoped references or short-lived credentials where supported.
- Revocation prevents later connector use.
- A worker cannot enumerate or use unrelated project credentials.
- Credential use produces audit evidence without serializing the secret.

## PF8 — Product operations gate
- A released test product can create a maintenance incident/task from approved error/health evidence.
- A fix is implemented in isolation, regression-tested and released as a new version.
- Dependency/security update work follows the same gates.
- Rollback to a prior known-good release is possible when the product architecture supports rollback.

## PF9 — Business Factory gate
Using a controlled non-spam test/sandbox scenario:
- business goal produces market/opportunity research;
- an opportunity can become a lead/work order only through allowed platform/account policy;
- a work order creates/links a ProductProject;
- Product Factory delivers the requested artifact/product;
- communication/delivery/payment state is durable and auditable;
- the business agent cannot expand financial/account authority or misrepresent the user's identity.

## PF10 — IP/license/compliance gate
- competitor research records only permitted public evidence;
- no proprietary source/assets/credentials are copied into the generated product without explicit legal basis;
- dependencies include version/license/provenance;
- distribution obligations/third-party notices are generated where applicable;
- an unacceptable license or missing provenance blocks release.

## PF11 — Representative end-to-end factory gate
From a clean packaged Windows Nika installation, issue a product request such as:

`Create an accessible Windows personal-expense application. Research alternatives first, propose a design, create the repository after the required decision, implement it, test it and prepare the Windows release.`

Without manual source-code copy/paste, evidence must cover:
1. research;
2. product decision;
3. durable ProductProject;
4. acceptance criteria;
5. team composition;
6. repository creation/connection;
7. isolated implementation;
8. independent QA/audit;
9. accessibility evidence;
10. build/package;
11. release provenance/checksums/licenses;
12. restart/resume;
13. clear human-only acceptance items.

This is the minimum proof that Nika is a product factory rather than only a coding assistant.

## PF12 — Long-horizon project gate
A multi-session test project must prove that ProductProject state remains coherent across repeated application restarts and independent worker cycles, including blocked work, resumed work, superseded specifications, release versions and maintenance events. Completion is based on acceptance evidence, not the number of agents or commits.
