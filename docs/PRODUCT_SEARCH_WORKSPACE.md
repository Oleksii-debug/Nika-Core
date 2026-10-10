# Product Search workspace/profile

Status: SLOT6 production-domain candidate. This document does not claim packaged UI completion.

## Purpose

Product Search is a thin domain projection over the existing Universal Research Engine. It does not add another crawler, HTTP stack, browser agent, database, model provider, or checkout flow.

The core flow in this batch is:

`ResearchResultSet + provenance-bound ProductObservation -> ProductSearchResult -> accessible text / durable JSON`

A repeated-run comparison is also available:

`previous ProductSearchResult + current ProductSearchResult -> deterministic new/changed ProductSearchDelta`

Product Search is read-only discovery. Purchasing, checkout, account mutation, payment, and other external side effects are outside this capability.

## REUSE -> ADAPT -> CUSTOM (thin)

**REUSE**
- `nika_core.research` result, evidence, source-kind, freshness, and query contracts.
- Python `Decimal` for exact recorded prices and `json` for the portable deterministic codec.
- Existing Nika task/runtime/persistence layers remain the owners of scheduling and durable job state.

**ADAPT**
- Research result items are projected into product cards only when structured product facts bind to the exact `source_id + locator + observed_at` evidence already present on the research item.
- Research rank is reused as the default deterministic relevance order.
- Existing evidence is retained on every card instead of creating parallel provenance.
- Repeated Product Search snapshots are compared by stable `product_id` so scheduled callers can surface new/materially changed products without requiring an LLM.

**CUSTOM (thin)**
- Product identity, seller, price/currency, availability, product filters, deterministic price sort, and accessible text labels.
- A strict schema-versioned JSON codec for restart/handoff boundaries.
- Material product-delta semantics for title, seller, price, currency, availability, source identity, and locator.

No new dependency is introduced.

## Correctness boundaries

- Product facts are not guessed from snippets. A caller/extractor must provide a structured `ProductObservation`.
- An observation whose document or exact evidence tuple is absent from the research result fails closed.
- `price_amount` and `currency` must occur together. Currency is normalized to a three-letter ASCII code.
- There is no implicit FX conversion. Price filtering/sorting requires one explicit currency; observations in other currencies or without a price are excluded from that operation.
- Prices are finite, non-negative `Decimal` values.
- Duplicate `product_id` values fail closed.
- Availability filtering is explicit; no model decides whether an item is purchasable.
- JSON decoding rejects unsupported schema versions and unknown fields.
- Delta comparison requires the same workspace, query, and normalized criteria.
- Rank/snippet/evidence-observation timestamp churn alone is not a material product change.
- The delta intentionally reports only **new** and **changed** products. It does not claim that a product is removed merely because it disappeared from a bounded top-N result set; a future complete-catalog/removal proof must come from an upstream source that can establish absence.

## Accessibility

`ProductSearchService.render_text()` emits linear text with a stable field order: title, seller, price, availability, match reason, source, observation time, and snippet. `ProductDeltaService.render_text()` provides a separate linear new/changed report with explicit changed-field names and before/after price or availability where relevant. Neither requires color, cards, pointer interaction, spatial interpretation, or a visual table.

This is backend accessibility evidence only. `HUMAN_TESTED=false` and `NVDA_VERIFIED=false` until a real Windows/NVDA Product Journey is exercised by a human.

## Privacy and safety

The package performs no network calls and no provider calls. It serializes only the product card and the research evidence already supplied to it; it does not capture credentials, cookies, browser profiles, environment variables, or raw HTTP diagnostics. Secret-sanitization guarantees remain owned by the upstream research/evidence boundary.

## Acceptance in this batch

Required evidence before integration:

1. Ruff / formatting policy passes.
2. Python compilation/import succeeds.
3. Focused Product Search and repeated-run delta tests pass.
4. Core CI passes on the exact branch head.
5. No files outside the SLOT6 ownership paths are changed.
6. An independent compatibility/integration review is still required by project parallel-work rules.

A packaged UI claim is explicitly out of scope for this isolated batch; Product Journey credit is not granted until a later compatible UI integration uses this service end to end.
