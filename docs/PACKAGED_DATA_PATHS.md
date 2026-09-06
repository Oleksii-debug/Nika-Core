# Packaged runtime data paths

Status: runtime contract for the Windows packaged application.

## Database authority

`AppConfig.database_path` has three authority levels, in this order:

1. `NIKA_DB_PATH` when explicitly supplied;
2. legacy alias `NIKA_DATABASE_PATH` when the primary alias is absent;
3. the runtime-mode default.

Explicit paths are not rewritten. This keeps tests, controlled release proofs and operator-selected data locations deterministic.

## Runtime-mode default

Python/source development keeps the historical relative default `./data/nika_core.db`. This avoids changing repository-local developer behavior.

A frozen packaged runtime uses the already-declared `platformdirs` dependency to select a user-writable, working-directory-independent data directory and stores `nika_core.db` there. With the Nika application name and no redundant author segment, the normal Windows location is under `%LOCALAPPDATA%\NikaCore\nika_core.db`.

The packaged path intentionally has no application-version directory so state can survive ordinary Nika upgrades. `SQLiteStore` remains responsible for creating missing parent directories and applying schema migrations when the database is opened.

## Compatibility boundary

This change does not silently copy or migrate a historical CWD-relative database into the packaged user-data location. Silent discovery would make an arbitrary launch directory part of durable state authority and could select the wrong database. If a controlled test or development workflow needs a specific existing database, it must set `NIKA_DB_PATH` explicitly.

The M11 packaged ProductProject proof already follows that rule by assigning a temporary explicit `NIKA_DB_PATH`; this contract therefore does not change its evidence semantics.

No credential, secret, permission, ProductProject schema, SQLite schema or release authority is introduced here.
