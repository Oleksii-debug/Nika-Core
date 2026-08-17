from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppConfig:
    app_version: str
    database_path: Path
    log_level: str
    model_provider: str

    @classmethod
    def from_environment(cls) -> AppConfig:
        db = Path(os.environ.get("NIKA_DB_PATH", "./data/nika_core.db"))
        return cls(
            app_version="0.0.1",
            database_path=db,
            log_level=os.environ.get("NIKA_LOG_LEVEL", "INFO").upper(),
            model_provider=os.environ.get("NIKA_MODEL_PROVIDER", "mock").lower(),
        )
