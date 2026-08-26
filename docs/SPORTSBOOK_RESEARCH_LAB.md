# Sportsbook Research Lab foundation

This lane is a provider-neutral, read-only research/data capability. It does **not** expose wager placement, deposits, withdrawals, funding, account control, or any real-money execution surface.

## Authority and data model

Stable IDs cover source, competition, participant, event, market and selection. Observations cover odds, score, period state and settlement. Decimal values are persisted as canonical text rather than binary floating point.

Every observation carries three UTC clocks:

- `event_at`: when the real-world/provider fact says the change occurred;
- `source_at`: when the upstream source published or observed it;
- `available_at`: when Nika could actually have known it.

The invariant is `event_at <= source_at <= available_at`. Historical/as-of reads are bounded by `available_at`, preventing research/backtest look-ahead from later-arriving data.

## Durability and recovery

`SportsbookRepository` uses the existing Nika connection-store shape and owns a separate `sportsbook_schema_migrations` namespace, avoiding edits to shared migrations. Exact identity replay is idempotent; replaying the same identity with changed immutable content fails closed. Provider cursors and each downloaded batch commit in one `BEGIN IMMEDIATE` transaction so restart/resume does not advance a cursor without the corresponding observations.

## Extension boundary

`SportsbookDataPort.fetch_updates()` is the only provider contract in this foundation. Concrete HTTP/browser/vendor adapters remain optional follow-up work and must preserve provenance, credential isolation, rate limits and terms/licensing. Any future wager execution capability would require a separately owned architecture/security/approval design and is explicitly out of scope here.
