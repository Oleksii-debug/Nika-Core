# Knowledge Corpus / deterministic retrieval

MANUAL-DEV24 owns the approved local knowledge persistence and retrieval boundary. Universal
Research remains the canonical acquisition/evidence engine; this layer consumes approved text and
provenance rather than duplicating fetching, browser automation, PDF/DOCX/XLSX parsing, or research
report generation.

## Architecture decision

Decision order is **REUSE -> ADAPT -> CUSTOM (thin)**.

- **REUSE:** Python `sqlite3`, the existing canonical `SQLiteStore`, existing `normalize_text`, and
  the existing deterministic research chunker. SQLite FTS5 provides indexing, `MATCH`, `snippet()`
  and BM25 ranking. Existing maintained document extractors remain in
  `nika_core.research.documents`.
- **ADAPT:** the existing chunker now exposes exact character spans while preserving the old
  `chunk_text()` API. `SQLiteStore` initializes an independently versioned knowledge schema using
  the same pattern already used for ProductProject schema ownership.
- **CUSTOM (thin):** Nika-owned artifact/version/provenance/ACL contracts, immutable chunk hashes,
  current-version selection, source-identity validation, integrity checks, and a small deterministic
  retrieval-evaluation harness.
- **Not adopted:** Qdrant/vector search. The current deterministic evaluation fixture reaches
  recall@3=1.0 and hit@1=1.0 for its exact-term corpus. A vector adapter may be proposed later only
  if a broader curated semantic-query evaluation demonstrates a material retrieval benefit. It may
  never become authoritative product state.

SQLite FTS5 behavior is based on the maintained upstream documentation at
`https://www.sqlite.org/fts5.html`. `UNINDEXED` metadata columns stay out of the token index;
`bm25()` assigns numerically lower scores to better matches; deterministic tie-breakers are added
by Nika after the score.

## Durable identity and versioning

A knowledge artifact is identified by `(workspace_id, artifact_key)`. `artifact_key` is a stable
source/artifact identity supplied by the approved ingestion caller; it is not derived from mutable
content. Each changed ingestion creates an immutable integer version. The current version is stored
on `knowledge_artifacts` and is switched in the same `BEGIN IMMEDIATE` transaction that writes the
version, chunks, FTS rows and ACL.

A retry is deduplicated only when the complete current-version identity is unchanged: normalized
text hash, optional raw SHA-256, title/media type, source identity/locator, parser name/version,
approval reference, normalization algorithm identity, and chunker policy/algorithm identity. Any
change to those fields, or a later reversion to historically seen content, creates a new immutable
version. Historical versions remain available for audit/recovery, while normal retrieval only
exposes the current version.

`raw_sha256` is optional because some approved upstream handoffs contain extracted text but not the
original bytes. When supplied it is validated as lowercase SHA-256 and preserved as provenance; the
corpus does not pretend it recomputed a hash for bytes it did not receive.

## Approved ingestion and provenance

Every new version records:

- workspace and stable artifact identity;
- normalized SHA-256 and optional upstream raw SHA-256;
- title/media type;
- source ID and source locator;
- parser/extractor name and version;
- system-owned normalization algorithm version;
- system-owned chunker algorithm version and exact max/overlap policy;
- `approved_by` authority reference;
- normalized text and immutable creation time.

When `source_id` is supplied, ingestion validates it against the authoritative Universal Research
source registries before any corpus row is written. Exactly one durable source identity must exist,
it must belong to the same workspace, and its durable locator must match the requested locator.
Cross-workspace source reuse fails with `PermissionError`; missing, ambiguous, or locator-mismatched
source identity fails with `ValueError`. This closes the AUD03 cross-workspace provenance attack
without importing DEV23 implementation or creating a second source registry.

A handoff may omit `source_id` when it has no registered Universal Research source identity, but it
must still provide an approved opaque `source_locator`. That locator is provenance only; it is not a
filesystem/network authority grant.

Each chunk records deterministic ordinal, exact `[start_char, end_char)` boundaries, chunk SHA-256
and a deterministic chunk ID framed from workspace/artifact/version/ordinal/hash. Result provenance
contains all of those fields so a caller can trace the returned text to the exact persisted version.

The corpus expects already-extracted approved text. Format parsing continues to reuse
`nika_core.research.documents` and DEV05 media transcription/subtitle/OCR handoffs rather than
creating a second parser/media framework here.

## Permissions before intelligence

`KnowledgeCorpus.search()` requires a host-owned `RetrievalScope` containing the principal identity
and the already-authorized workspace namespace set. The SQL statement applies all of these filters
before selected chunk text is returned:

1. FTS match;
2. workspace must be in the authorized scope;
3. only the artifact current version may match;
4. workspace-visible artifacts are allowed inside that namespace;
5. restricted artifacts additionally require an exact `(workspace, artifact, principal)` ACL row.

The filtered result is the only material intended for Deterministic Brain, local models, Ollama or
cloud/API providers. Intelligence providers do not receive an unfiltered candidate set and must not
be allowed to construct a broader host `RetrievalScope` themselves.

A byte-identical duplicate retry is not allowed to silently change visibility or ACL grants. ACL
changes may accompany a new approved version, where they commit atomically with that version.

## Deterministic FTS ranking

User queries are normalized, split into terms, escaped as FTS phrases and joined with logical `AND`.
This prevents raw FTS syntax from becoming the query language. Results order by SQLite BM25 rank,
then stable artifact/version/chunk identities, so score ties are deterministic. Search limits are
strict integers bounded to 1..100; Python Boolean aliases are rejected.

The evaluation harness in `retrieval_evaluation.py` accepts explicit query/scope/expected-artifact
cases and reports recall-at-limit plus hit-at-1. It uses the production permission-filtered search
path, so restricted data cannot receive evaluation credit for an unauthorized scope. Multiple
matching chunks from one artifact are collapsed to one artifact identity before recall is computed,
so chunk fan-out cannot inflate retrieval quality above the actual artifact-level result.

## Migration and restart

Knowledge schema migrations are independently ordered in `knowledge_schema_migrations` and are
initialized by the canonical `SQLiteStore` after the main and ProductProject schemas. A database
with a newer knowledge schema fails closed.

Migration v2 non-destructively projects legacy v9 `corpus_documents` into artifact keys of the form
`legacy:<document_id>`. The old tables are retained. Because legacy rows did not persist reliable
character offsets for every historical chunk, each migrated legacy document is represented by one
exact full-document chunk with boundaries `[0, len(normalized_text))`. Existing source locator/ID is
retained when present. New ingestions use bounded overlapping chunks with exact offsets.

Restart requires no in-memory reconstruction: current version, history, ACL, chunks, hashes and FTS
rows are all durable SQLite state. Concurrent ingesters serialize through `BEGIN IMMEDIATE`, so an
exact duplicate race produces one immutable version plus one deduplicated retry rather than two
versions.

## Corruption behavior

`verify_integrity()` runs SQLite `PRAGMA quick_check` and then verifies:

- every artifact current-version pointer exists;
- normalized text matches its stored SHA-256;
- every chunk has a valid parent version;
- chunk text matches its SHA-256 and exact source-text boundaries;
- every authoritative chunk has exactly one matching FTS row;
- FTS title/body and metadata match the authoritative version/chunk;
- no orphan, duplicate, missing, or stale FTS row exists.

Search independently rechecks the selected version hash, FTS title/body, chunk hash, and boundaries
before returning a hit. Corrupt material fails closed with `CorpusCorruptionError`; it is not passed
to an intelligence provider.

## Acceptance evidence

Required repository evidence for this batch is the exact-head `scripts/verify.py` contract:
`pip check`, Ruff, compileall and the complete pytest suite on Ubuntu and Windows. The focused tests
cover duplicate/change/reversion/provenance-policy versioning, restart, concurrent duplicate ingest,
ACL/workspace isolation, DEV23 source-registry provenance binding, the AUD03 cross-workspace oracle,
deterministic tie ranking, literal query handling, provenance and chunk boundaries, version/chunk/
FTS corruption, injected transaction rollback, legacy migration, future-schema rejection,
artifact-distinct retrieval evaluation, Boolean-limit rejection, and real `SQLiteStore` integration.

`HUMAN_TESTED=false`. `NVDA_VERIFIED=false`. This backend batch does not claim a human/NVDA gate.
