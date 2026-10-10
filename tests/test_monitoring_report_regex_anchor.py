import pytest

from nika_core.research.monitoring_report import _optional_code, _safe_reference


def test_safe_code_and_reference_accept_representative_normal_values() -> None:
    assert _optional_code("changed", "kind") == "changed"
    assert _optional_code("condition_matched", "terminal_reason") == "condition_matched"
    assert _safe_reference("source-news", "source_id") == "source-news"
    assert _safe_reference("result-set.v1", "result_set_id") == "result-set.v1"


def test_safe_code_and_reference_reject_literal_regex_anchor_text() -> None:
    with pytest.raises(ValueError, match="bounded safe code"):
        _optional_code(r"changed\Z", "kind")
    with pytest.raises(ValueError, match="bounded safe reference"):
        _safe_reference(r"source-news\Z", "source_id")
