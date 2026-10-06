from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.contracts import MediaResourceClaim, ResourceClass
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.resources import MediaResourceCoordinator
from nika_core.resources.contracts import ResourceSnapshot
from nika_core.resources.manager import ResourceManager


class Observer:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=10.0,
            memory_percent=20.0,
            available_memory_bytes=8_000_000_000,
        )


def test_heavy_model_concurrency_above_one_requires_separate_physical_proof(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    observer = Observer()
    coordinator = MediaResourceCoordinator(ResourceManager(store, observer), observer)
    claim = MediaResourceClaim(
        claim_id="parallel-heavy",
        owner_id="transcription",
        resource_class=ResourceClass.HEAVY_MODEL,
        max_concurrent=2,
    )

    with pytest.raises(MediaError, match="target-machine proof") as caught:
        coordinator.request(claim)
    assert caught.value.code == MediaErrorCode.RESOURCE_BLOCKED
    assert caught.value.retryable is False
