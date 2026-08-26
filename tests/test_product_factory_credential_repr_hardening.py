from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nika_core.product_factory_credentials import CredentialBroker

RAW_SECRET = "repr-only-test-secret-never-serialize"


@dataclass(slots=True)
class _DangerousStoreRepr:
    raw_material: str = RAW_SECRET

    def contains(self, secret_ref: str, generation: int) -> bool:
        del secret_ref, generation
        return False

    def issue_handle(
        self,
        *,
        secret_ref: str,
        generation: int,
        project_id: str,
        audience: str,
        scopes: frozenset[str],
        expires_at: datetime,
    ) -> str:
        del secret_ref, generation, project_id, audience, scopes, expires_at
        raise AssertionError("not used")

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        del secret_ref, generation


def test_broker_repr_never_delegates_to_protected_store_repr() -> None:
    store = _DangerousStoreRepr()
    assert RAW_SECRET in repr(store)

    broker = CredentialBroker(store)

    assert RAW_SECRET not in repr(broker)
    assert "raw_material" not in repr(broker)
