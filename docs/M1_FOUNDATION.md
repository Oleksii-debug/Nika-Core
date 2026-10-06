# M1 Foundation — reuse decisions and evidence plan

Date: 2026-08-18

## Scope
This milestone turns the bootstrap into a durable extensible product foundation: validated settings, forward-only schema migrations, persisted agent/workspace registries, audit history, workspace discovery contract and user-remappable application shortcuts.

## REUSE / ADAPT / CUSTOM
- REUSE: Pydantic Settings for typed `NIKA_*` configuration and validation.
- REUSE: Python sqlite3 for the local embedded database.
- CUSTOM: a deliberately small ordered migration runner because the current schema is SQLite-only and does not yet justify SQLAlchemy/Alembic. Re-evaluate at complex table-rewrite or multi-database requirements.
- REUSE: `importlib.metadata.entry_points()` for installed workspace discovery.
- CUSTOM: Nika Agent Registry, Workspace Registry, Audit Log and Action Registry/Keymap because their versioning, safety and accessibility semantics are product-specific.

## Migration safety
New databases apply migrations in order. Existing schema version 1 databases upgrade to version 2. Databases with a schema version newer than the running application fail closed. Each write uses the SQLiteStore connection transaction boundary.

## Registry evolution
Agent and workspace definitions are append-versioned. Re-registering the same or an older version is rejected. Reads return the latest version, preserving prior definitions for audit/recovery.

## Keyboard architecture
Actions have stable dotted IDs independent of visible labels and shortcuts. Defaults are registered centrally. Keymap overrides persist in SQLite; users can remap, explicitly unbind allowed actions, restore defaults, export/import a versioned JSON representation and are blocked from same-scope shortcut conflicts.

## Workspace extension boundary
Independent Python packages advertise workspaces through the `nika_core.workspaces` entry-point group. Discovery does not eagerly import packages. Loading and activation will later add compatibility, permissions and security validation.

## Required evidence
- configuration environment loading and invalid-value rejection;
- new database reaches schema 2;
- real version-1 fixture upgrades to schema 2;
- future/newer schema is rejected;
- agent/workspace versions persist across registry instances;
- audit event round trip;
- action duplicate/default conflict rejection;
- keymap remap, unbind, restore, persistence, import/export and atomic conflict rejection;
- existing task/checkpoint tests remain green;
- Ruff, compileall and pytest pass in PR CI.
