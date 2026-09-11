# Permission-scoped SQLite FTS ranking

MANUAL-DEV24 keeps SQLite FTS5 as the deterministic retrieval engine, but ranking statistics must be computed only from material the caller is authorized to retrieve.

## Defect

The durable `knowledge_fts` table is current-version-only, but it contains current rows for every workspace and every ACL principal. SQLite FTS5 `bm25()` computes corpus statistics over the virtual table, not only the rows that survive an outer SQL workspace/ACL filter. Therefore inaccessible documents could alter scores and top-N ordering for otherwise identical authorized results without ever being returned themselves.

A deterministic regression demonstrates the effect: two authorized documents are initially tied for `alpha beta`; adding many `alpha` documents in another workspace changes their global-table BM25 weighting and reverses the ordering. The same contamination is possible from restricted artifacts that the current principal cannot access.

This violates the Knowledge boundary in `docs/MASTER_SPEC.md`: workspace/user permission filtering must happen before material reaches deterministic or model-based intelligence. Ranking itself is part of retrieval and therefore must not depend on inaccessible corpus state.

## Repair

Decision remains **REUSE -> ADAPT -> CUSTOM (thin)**.

- **REUSE:** SQLite FTS5 `MATCH`, `snippet()` and `bm25()`; the durable current-only `knowledge_fts` index; canonical workspace/ACL state.
- **ADAPT:** each `KnowledgeCorpus.search()` builds a connection-local TEMP FTS5 projection containing only current rows authorized by the supplied host-owned `RetrievalScope`. `MATCH`, snippets and BM25 execute only after that projection is populated.
- **CUSTOM (thin):** the SQL that intersects workspace scope and restricted-principal ACL before index projection. No custom ranking algorithm or generic RAG framework is introduced.

The temporary table is non-authoritative and disappears with the database connection. It is populated from durable `knowledge_fts`, rather than directly from chunks, so current FTS title/body corruption still reaches the existing authoritative version/chunk verification and fails closed.

The persistent index remains independently integrity checked and remains current-version-only. Immutable historical versions/chunks remain authoritative audit/recovery state outside ranking statistics.

## Isolation semantics

For one search:

1. validate the query, caller scope and result limit;
2. create a connection-local TEMP FTS5 table with the same indexed title/body fields and tokenizer;
3. copy only durable current FTS rows whose workspace is in `RetrievalScope.workspace_ids` and whose visibility is either workspace-wide or explicitly grants `RetrievalScope.principal_id`;
4. execute `MATCH`, `snippet()` and `bm25()` against that permission-scoped table;
5. join selected rows back to authoritative version/chunk state;
6. recheck version hash, indexed title/body, chunk hash and exact boundaries before returning text.

Documents outside the authorized workspace set and restricted documents without an exact principal ACL grant cannot affect document frequency, average length, BM25 score, tie ordering, snippets or returned text.

## Performance boundary

The TEMP projection is deliberately correctness-first. It rebuilds the authorized current corpus for each search connection. Do not replace it with a process-global cache unless cache identity is bound to the exact principal/workspace/ACL generation and deterministic invalidation is proven. Optimize only after profiling demonstrates a real bottleneck; permission isolation is not traded for faster ranking.

## Vector decision

No Qdrant/vector dependency is added. The current measured exact-term retrieval path is deterministic and sufficient for the present evaluation. Semantic/vector search remains optional only after a broader curated evaluation proves material benefit, and it may never replace authoritative SQLite product state or bypass the same permission boundary.

## Acceptance

Focused regressions require both of these invariants:

- adding many current documents in another workspace leaves an authorized principal's result order and BM25 scores unchanged;
- adding many restricted documents in the same workspace leaves an unauthorized principal's result order and BM25 scores unchanged, while an explicitly authorized principal can retrieve those documents.

Repository acceptance still requires exact-head dependency consistency, Ruff, compile and complete pytest on Ubuntu and Windows plus applicable M12. Independent AUD03 provenance replay remains separate.

`HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `PRODUCTION_RELEASE_READY=false`.
