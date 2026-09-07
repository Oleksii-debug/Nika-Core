# Table Tennis Stats Collector workspace

Status: isolated production workspace foundation. This document does not claim a user-reachable Product Journey.

## Purpose

The binding workspace reuse catalog defines Table Tennis Stats Collector as a small proof for source ingestion, deduplication, incremental change state, statistics, and accessible CSV/TXT reporting. It must reuse Universal Research/data primitives and must not create a second sports crawler architecture.

This slice implements only the domain boundary after canonical Research has already produced evidence. It contains no HTTP client, browser automation, provider-specific scraper, model call, account connector, or external side effect.

## REUSE -> ADAPT -> CUSTOM (thin)

### REUSE

- `SQLiteStore` remains the canonical durable local database boundary.
- `ResearchEvidence` remains the provenance input contract. The workspace accepts source identity and observation time from that contract rather than inventing its own crawler/source system.
- Python standard library `sqlite3`, `hashlib`, `json`, `csv`, and Unicode normalization are sufficient for this proof. No dependency is added.

### ADAPT

- Research evidence is adapted into a stable `(source_id, source_match_id)` match identity.
- Match facts are canonicalized and SHA-256 fingerprinted so unchanged re-observation deduplicates while changed facts append a new revision.
- Newer provenance may refresh the current document/observation pointer without manufacturing a new fact revision.
- Older evidence may confirm identical facts, but it cannot replace newer facts or regress the latest provenance pointer.

### CUSTOM (thin)

Workspace-specific code is limited to:

- table-tennis match/game validation;
- stable match identity and fact fingerprinting;
- append-only revision history and restart validation;
- deterministic player statistics;
- linear text and CSV serialization.

There is no generic crawler, scheduler, memory framework, model framework, or new migration framework in this workspace.

## Durable model

Workspace-local tables are initialized by `initialize_table_tennis_schema()` and are versioned separately from the shared Core migration sequence so this isolated lane does not edit a currently owned shared schema file.

For each source match, `table_tennis_matches` stores the stable identity, current version/fingerprint, and latest provenance pointer. `table_tennis_match_revisions` stores every accepted fact revision. A changed observation is admitted under SQLite writer reservation, written as the next revision, then promoted through a compare-and-set current pointer.

On restart, persisted payloads are treated as untrusted durable input. The repository validates:

- source identity still derives the stored match ID;
- version/history is contiguous;
- current fingerprint binds the current revision;
- payload hash matches the stored fingerprint;
- exact payload fields are known;
- game scores retain strict integer types rather than being coerced from strings/bools;
- timestamps are timezone-aware;
- players remain distinct and the completed match has a winner.

Malformed or tampered durable state fails closed with `TableTennisDataIntegrityError`.

## Provenance and privacy boundary

The workspace deliberately does **not** persist `ResearchEvidence.locator`. A locator may contain URL userinfo, query credentials, signed parameters, cookies encoded by a defective upstream adapter, or other material that does not belong in this domain database.

The durable domain stores only:

- `source_id`;
- source-local match ID;
- document ID;
- observation timestamp;
- normalized match facts and their fingerprint.

Synthetic-canary regression coverage verifies that credential-shaped locator content does not appear in the database bytes after ingestion.

This does not replace Universal Research's own provenance/security requirements. It is a data-minimization boundary for this workspace.

## Statistics

Statistics are recomputed deterministically from current accepted match revisions. The workspace currently reports per player:

- matches;
- wins/losses;
- games won/lost;
- win rate.

No LLM is required. A future model-powered explanation, if justified, must be an optional ModelGateway consumer over these deterministic facts and cannot become the statistics authority.

## Accessible output

`render_text_report()` produces a linear, labeled UTF-8 text representation suitable for screen-reader navigation and copying. It does not encode meaning through color, visual position, or unlabeled glyphs.

`render_csv_report()` emits stable columns and CRLF line endings. Player names beginning with `=`, `+`, `-`, or `@` are prefixed with an apostrophe before CSV serialization to prevent spreadsheet formula execution when the report is opened by a spreadsheet application.

Unicode NFC normalization is applied to textual domain fields, and tests exercise Cyrillic player/competition names and a Unicode database path.

## Integration boundary

This PR intentionally does not edit `src/nika_core/workspaces/catalog.py`, `src/nika_core/workspaces/__init__.py`, UI/UIA, shared Research source, shared migrations, workflows, dependencies, or permissions because those surfaces have active owners.

Therefore this slice can prove the reusable domain contract but cannot claim that a Windows/NVDA user can already open and operate Table Tennis Stats Collector from the packaged Nika UI.

After this isolated candidate is exact-green and independently reviewed, the next integration batch must coordinate with the incumbent workspace/UI owner to:

1. register the workspace through the canonical catalog/SDK path;
2. route allowed Research results into explicit match observations without a sports-specific crawler;
3. expose import/status/report actions through standard accessible controls and key bindings;
4. preserve Tool Registry/permission/audit boundaries;
5. add a representative packaged Windows Product Journey;
6. perform a real human NVDA test before any `NVDA_VERIFIED=true` claim.

## Acceptance truth

The isolated pre-push behavioral smoke is useful regression evidence only; it is not exact-repository acceptance. Production credit requires the exact branch head to pass repository dependency consistency, Ruff, compile, full pytest, and the applicable release gate without weakening any check. Integration credit additionally requires current-main compatibility, independent review where required, guarded integration, and post-merge exact-main evidence.

`HUMAN_TESTED=false`.

`NVDA_VERIFIED=false`.

`PRODUCTION_RELEASE_READY=false`.
