from __future__ import annotations

from nika_core.data.schema import MIGRATIONS as BASE_MIGRATIONS
from nika_core.data.schema import SCHEMA_VERSION as BASE_SCHEMA_VERSION
from nika_core.research.profile_schema import RESEARCH_PROFILE_MIGRATION_12

MIGRATIONS: dict[int, tuple[str, ...]] = dict(BASE_MIGRATIONS)
_existing = MIGRATIONS.get(12)
if _existing is not None and _existing != RESEARCH_PROFILE_MIGRATION_12:
    raise RuntimeError("schema migration 12 conflicts with the research profile migration")
MIGRATIONS.setdefault(12, RESEARCH_PROFILE_MIGRATION_12)
SCHEMA_VERSION = max(BASE_SCHEMA_VERSION, 12)
