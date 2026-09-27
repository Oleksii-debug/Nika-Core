# V0.1 Durable Previous Observation

Worker 55 owns a narrow V01-B05 durability proof. The implementation reuses the existing
Research SQLite state and does not add a scheduler, change detector, workflow engine, cache, or
second persistence layer.

## Contract

`DurablePreviousObservationLoader.load()` must reconstruct the previous monitoring observation
from canonical SQLite state on every call. The caller supplies the declared series, workspace,
profile version, and source-set version. The loader reads `research_profile_run_history`, reloads
the immutable profile/source-set definitions, reloads the referenced `ResearchResultSet` through
`NetworkResearchRepository`, and validates the bindings before returning evidence.

The loader intentionally has no process-local previous-observation field or cache. A new process
can create a new `SQLiteStore`, `ResearchProfileRepository`, `NetworkResearchRepository`, and
loader against the same database and recover the same result-set ID and result items.

The received `PreviousObservationExpectation` is itself an authority-bearing input. `load()`
requires the exact expectation carrier and reconstructs it through the canonical constructor
before any SQLite lookup. Series/workspace/profile/source-set identities must be exact built-in
strings, and profile/source-set versions must be exact positive built-in integers. Behavioral
primitive subclasses and constructor-bypassed exact-type objects therefore cannot participate in
identity equality or durable version comparisons.

## Fail-closed cases

The loader raises `PreviousObservationError` with a stable code instead of supplying an empty
baseline that could be misclassified as `changed`:

- `missing_baseline`: no durable history exists for the series.
- `corrupt_baseline`: persisted definitions/result data cannot be decoded or violate structural
  invariants. Durable history profile/source-set versions must retain exact positive SQLite/Python
  integer semantics; fractional REAL values are never coerced into trusted versions.
- `identity_mismatch`: profile, source-set, task/series, source evidence, query, or workspace
  binding does not match the declared monitor identity.
- `stale_version`: the latest durable observation belongs to another profile/source-set version.
- `duplicate_baseline`: more than one latest history row has the same canonical observation
  timestamp, so choosing a predecessor would be ambiguous.

`research_profile_run_history.result_set_id` is already unique in the canonical schema; the
additional equal-latest-timestamp check prevents the historical `ORDER BY ... task_id` tie-breaker
from silently choosing between two logically simultaneous candidates.

## Reuse / adapt / custom decision

- **REUSE:** `research_profile_run_history`, `research_profile_series_tasks`, versioned Research
  profiles/source sets, `research_result_sets/items`, `SQLiteStore`, and
  `NetworkResearchRepository.get_result_set()`.
- **ADAPT:** expose a strict read/validation adapter suitable for the monitor recurrence/change
  detector integration lanes.
- **CUSTOM (thin):** typed expectation/result/error objects and fail-closed binding checks only.

No packaged UI files are changed. No secrets, raw HTML, cookies, tokens, or authentication query
values are persisted or rendered by this slice.

## Acceptance proof scope

Focused tests cover restart with an unchanged or changed source, missing baseline, malformed
persisted evidence, wrong source identity, stale profile/source-set version, ambiguous duplicate
latest observations, cross-workspace result-set substitution, fractional durable version storage,
behavioral identity/version primitive subclasses, and exact-type constructor-bypassed expectation
objects. HUMAN_TESTED and NVDA_VERIFIED remain false; this backend slice does not claim human
accessibility evidence.


## Source-definition and canonical task authority

The previous observation is accepted only when the durable run-history task is still a canonical
completed `research.profile.run` TaskQueue record in the expected workspace and its task payload
pins the exact profile/source-set IDs and versions named by history and the caller.

Source identity is also validated beyond source ID/kind. Local-file evidence locator must equal the
current canonical local source locator. HTTP evidence must first match the canonical
`corpus_http_origins` / snapshot authority for the concrete result `document_id`; this prevents
same-task A/B final-URL substitution between different documents. The loader additionally requires
durable `research_http_attempts` provenance proving that this exact evidence locator was a
successful final URL reached from the *current declared URL* before the baseline result was
created. Retargeting the same HTTP source_id to another declared URL therefore invalidates the old
baseline, while legitimate historical redirects for an unchanged declared URL remain valid.

These checks reuse TaskQueue and Research repositories/tables. They add no cache, source registry,
scheduler, change detector, or monitoring state authority.
