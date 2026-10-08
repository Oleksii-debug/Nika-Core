from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika_core.ui.bridge import UIActionBridge


def _bridge(provider) -> UIActionBridge:
    return UIActionBridge(SimpleNamespace(), SimpleNamespace(), state_provider=provider)


def test_state_snapshot_is_json_native_and_detached_from_provider() -> None:
    original = {"tasks": [{"task_id": "task-1", "state": "READY"}]}
    bridge = _bridge(lambda: original)
    response = bridge.get_state()
    assert response == {"ok": True, "state": original}

    original["tasks"][0]["state"] = "FAILED"
    original["tasks"].append({"task_id": "task-2", "state": "READY"})
    assert response["state"] == {"tasks": [{"task_id": "task-1", "state": "READY"}]}


@pytest.mark.parametrize(
    "bad_state",
    [
        {"tasks": [object()]},
        {"tasks": [float("nan")]},
        {"tasks": ["x" * 1_048_577]},
        {7: "non-string-key"},
    ],
)
def test_state_snapshot_rejects_unserializable_or_unbounded_data(
    bad_state: dict[object, object], caplog: pytest.LogCaptureFixture
) -> None:
    bridge = _bridge(lambda: bad_state)
    assert bridge.get_state() == {
        "ok": False,
        "message": "Не вдалося отримати стан програми через внутрішню помилку.",
    }
    assert "Desktop state provider failed" in caplog.text


def test_state_snapshot_rejects_recursive_projection() -> None:
    cyclic: dict[str, object] = {}
    cyclic["cycle"] = cyclic
    response = _bridge(lambda: cyclic).get_state()
    assert response["ok"] is False
    assert "state" not in response


def test_expected_provider_error_does_not_expose_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "read-state-secret-canary"

    def error():
        raise ValueError(secret)

    response = _bridge(error).get_state()
    assert response == {
        "ok": False,
        "message": "Не вдалося отримати стан програми через внутрішню помилку.",
    }
    assert secret not in str(response)
    assert secret not in caplog.text
    assert "ValueError" in caplog.text


def test_state_snapshot_preserves_shutdown_signal() -> None:
    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _bridge(interrupted).get_state()


def test_state_provider_rejects_pair_iterable_without_invoking_user_iteration() -> None:
    class PairLike:
        def __init__(self) -> None:
            self.invocations = 0

        def __iter__(self):
            self.invocations += 1
            yield ("tasks", [])

    pair_like = PairLike()
    response = _bridge(lambda: pair_like).get_state()
    assert response == {
        "ok": False,
        "message": "Не вдалося отримати стан програми через внутрішню помилку.",
    }
    assert pair_like.invocations == 0
    assert "state" not in response


def test_state_provider_rejects_non_plain_mapping_before_serialization() -> None:
    from types import MappingProxyType

    response = _bridge(lambda: MappingProxyType({"tasks": []})).get_state()
    assert response["ok"] is False
    assert "state" not in response
