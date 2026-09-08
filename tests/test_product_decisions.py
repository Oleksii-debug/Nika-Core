from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier

import pytest

import nika_core.data.sqlite as sqlite_store_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_decisions import ProductDecisionRepository
from nika_core.product_project import (
    EvidenceRef,
    ProductDecision,
    ProductDecisionState,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    ResearchEvidencePackage,
    StaleProjectVersionError,
)
from nika_core.product_project_schema import (
    PRODUCT_PROJECT_MIGRATIONS,
    PRODUCT_PROJECT_SCHEMA_VERSION,
)


def _spec() -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build accessible expense app",
        desired_outcome="A tested durable product",
        requirements=(
            ProductRequirement(
                "req-1",
                "Keyboard operation",
                ("All primary actions keyboard reachable",),
            ),
        ),
    )
