# Sportsbook Research Foundation

## Scope

This module is a **read-only research/data capability** for sports events and sportsbook-style
odds observations. It is not a bookmaker automation system and carries no wager, account,
payment, deposit, withdrawal, funding, credential-redemption, or real-money execution authority.

The first production slice provides immutable domain identities for `Sport`, `Competition`,
`Event`, `Participant`, `Market`, `Selection`, and `SportsbookSource`, plus causal observations:
`OddsSnapshot`, `ScoreState`, `PeriodState`, `EventStatus`, and `Settlement`.

## REUSE -> ADAPT -> CUSTOM (thin)

- **REUSE:** `trading_research.contracts.EventTime` and its UTC/availability authority, Python
  `Decimal`, stdlib `sqlite3`, SHA-256, and the canonical `nika_core.data.sqlite.SQLiteStore`
  connection host.
- **ADAPT:** the existing research causality rule (`available_at` gates visibility) to sports-event,
  market, score, period, status, and settlement data.
- **CUSTOM (thin):** sportsbook-specific immutable identities, graph validation, one module-owned
  SQLite extension schema, semantic conflict/dedup keys, deterministic codecs, and read-only ports.

No new dependency, second database, generic event framework, model/LLM dependency, browser
credential path, or external-effect executor is introduced.

## Causality contract

Every observation reuses `EventTime(event_at, source_at, available_at)`:

- `event_at`: when the source says the sports fact/quote applies;
- `source_at`: optional timestamp emitted by the source;
- `available_at`: earliest time Nika may use the observation in research.

`observations_at(t)` filters **only by `available_at <= t`**. A record with an earlier `event_at`
but later `available_at` remains invisible until its actual availability boundary, preventing the
basic future-leakage pattern common in historical odds/sports datasets.

All timestamps are normalized to aware UTC by the reused trading-research contract.

## Numeric and identity rules

- Decimal odds are stored as exact decimal strings and must be finite and greater than `1`.
- Settlement values are exact finite decimals in `[0, 1]`.
- Score values and source sequence values are non-negative exact integers; booleans are rejected.
- Catalog identities are immutable. Re-registering the same identity with the same semantic bytes
  is idempotent; different bytes fail closed.
- Catalog entity collections are canonicalized by stable identity before use/persistence so caller
  input ordering cannot change restart equality or deterministic serialization behavior.
- Normalized observation-map keys must remain unique; whitespace-normalized collisions fail closed
  instead of silently overwriting a value.
- Observation identity binds observation type, source, subject, `event_at`, `source_at`, and source
  sequence. Exact replay is idempotent; a different payload at the same identity fails closed.
- Batch ingestion is one SQLite transaction, so a later conflict rolls back earlier new rows in the
  same batch.
- Schema/catalog/observation write paths reserve the SQLite writer with `BEGIN IMMEDIATE`, so two
  concurrent exact replays serialize into one durable insert rather than a read-before-insert race.

## Persistence and restart

`SQLiteSportsbookRepository` accepts the structural connection subset implemented by the canonical
`SQLiteStore`; product composition should pass the normal Nika store, including Unicode/spaced
Windows paths. The module uses tables inside that same database and owns only
`sportsbook_schema_migrations` plus `sportsbook_*` tables. It does not modify shared core or
ProductProject migration files.

Schema migration history must be contiguous and future versions fail closed. Catalog and
observation JSON are stored with SHA-256 integrity evidence and are revalidated before decoding.
Restart reconstruction therefore does not trust caller memory or candidate-supplied state.

## Provider boundary

`SportsbookSourcePort` is deliberately read-only:

- `source()` identifies the provider/provenance boundary;
- `read_catalog()` returns domain identity metadata;
- `read_observations()` returns source observations.

`SportsbookSource.source_uri` is provenance metadata, not a credential container. User-info
credentials and known sensitive query-parameter names such as access tokens, API keys, passwords,
sessions, cookies, or authorization values are rejected fail closed before the source identity can
enter the catalog.

There is intentionally no method for placing a wager, creating/funding an account, deposits,
withdrawals, redeeming credentials, or invoking bookmaker actions. A future adapter that reads
licensed/public data must stay behind this port and preserve source/license provenance.

## Acceptance boundary

This slice can claim only a deterministic local research foundation after exact-head Core/M12 and
independent review. It does **not** claim live provider integration, bookmaker correctness,
production release readiness, HUMAN_TESTED, or NVDA_VERIFIED. Any future live-data adapter needs
its own licensing/privacy/network review and separate acceptance evidence.
