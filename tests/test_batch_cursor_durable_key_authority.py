from __future__ import annotations

import pytest

from nika_core.batch_cursor import BatchCursorStateError, _decode_completion_result


class ArmedDurableText(str):
    armed = False

    def __hash__(self) -> int:
        if self.armed:
            raise AssertionError("durable key hash must not execute")
        return super().__hash__()

    def __eq__(self, other: object) -> bool:
        if self.armed:
            raise AssertionError("durable key equality must not execute")
        return super().__eq__(other)


def test_durable_completion_decoder_rejects_behavioral_outer_key_before_hash() -> None:
    key = ArmedDurableText("__nika_batch_cursor_completion_v1__")
    payload = {
        key: {
            "result": {"ok": True},
            "next_batch_not_before": None,
        }
    }
    key.armed = True

    with pytest.raises(BatchCursorStateError, match="completed effect result is malformed"):
        _decode_completion_result(payload)


def test_durable_completion_decoder_rejects_behavioral_envelope_key_before_hash() -> None:
    key = ArmedDurableText("result")
    envelope = {
        key: {"ok": True},
        "next_batch_not_before": None,
    }
    payload = {"__nika_batch_cursor_completion_v1__": envelope}
    key.armed = True

    with pytest.raises(BatchCursorStateError, match="completed effect envelope is malformed"):
        _decode_completion_result(payload)
