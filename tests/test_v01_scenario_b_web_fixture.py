from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections import Counter
from html.parser import HTMLParser

from test_support.v01_scenario_b_web_fixture import (
    SCENARIO_B_TARGETS,
    FixtureFamily,
    ScenarioBFixtureServer,
    scenario_b_manifest,
)


class _SemanticParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.tags.append((tag, dict(attrs)))


def _request(
    url: str,
    *,
    method: str = "GET",
) -> tuple[int, dict[str, str], bytes, str]:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return (
                response.status,
                dict(response.headers.items()),
                response.read(),
                response.geturl(),
            )
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read(), exc.geturl()


def _json(url: str) -> dict[str, object]:
    status, _, payload, _ = _request(url)
    assert status == 200
    parsed = json.loads(payload.decode("utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_manifest_has_exactly_20_stable_targets_and_two_per_family() -> None:
    manifest = scenario_b_manifest()
    assert len(manifest) == 20
    assert [item["input_order"] for item in manifest] == list(range(1, 21))
    assert [item["target_id"] for item in manifest] == [
        f"scenario-b-{index:02d}" for index in range(1, 21)
    ]
    assert len({item["target_id"] for item in manifest}) == 20
    assert Counter(item["family"] for item in manifest) == Counter(
        {family.value: 2 for family in FixtureFamily}
    )
    assert [target.target_id for target in SCENARIO_B_TARGETS] == [
        item["target_id"] for item in manifest
    ]
    assert [item["retry_safe"] for item in manifest[-2:]] == [False, False]


def test_server_is_loopback_only_and_manifest_contains_relative_target_paths() -> None:
    with ScenarioBFixtureServer() as fixture:
        assert fixture.base_url.startswith("http://127.0.0.1:")
        manifest = _json(f"{fixture.base_url}/manifest.json")
        assert manifest["scenario"] == "B"
        targets = manifest["targets"]
        assert isinstance(targets, list)
        assert len(targets) == 20
        for item in targets:
            assert isinstance(item, dict)
            path = item["path"]
            assert isinstance(path, str)
            assert path.startswith("/targets/")
            assert "://" not in path


def test_every_target_page_has_named_semantic_main_status_and_no_external_reference() -> None:
    with ScenarioBFixtureServer() as fixture:
        for target in SCENARIO_B_TARGETS:
            status, _, payload, _ = _request(fixture.target_url(target.target_id))
            assert status == 200
            html = payload.decode("utf-8")
            assert "chatgpt" not in html.casefold()
            assert "http://" not in html.casefold()
            assert "https://" not in html.casefold()

            parser = _SemanticParser()
            parser.feed(html)
            mains = [attrs for tag, attrs in parser.tags if tag == "main"]
            statuses = [
                attrs
                for _, attrs in parser.tags
                if attrs.get("role") == "status" and attrs.get("aria-label") == "Target status"
            ]
            assert mains == [
                {
                    "role": "main",
                    "aria-label": f"Scenario B target {target.target_id}",
                }
            ]
            assert len(statuses) == 1
            assert statuses[0]["data-target-id"] == target.target_id


def test_immediate_success_records_one_attempt_and_one_effect() -> None:
    with ScenarioBFixtureServer() as fixture:
        target_id = "scenario-b-01"
        status, _, payload, _ = _request(
            f"{fixture.base_url}/actions/{target_id}",
            method="POST",
        )
        assert status == 200
        assert "data-state='succeeded'" in payload.decode("utf-8")
        state = _json(fixture.state_url(target_id))
        assert state["attempt_count"] == 1
        assert state["effect_count"] == 1


def test_temporary_busy_is_retryable_before_effect_then_succeeds_once() -> None:
    with ScenarioBFixtureServer() as fixture:
        target_id = "scenario-b-05"
        first_status, _, first_payload, _ = _request(
            f"{fixture.base_url}/actions/{target_id}",
            method="POST",
        )
        assert first_status == 503
        assert "data-state='temporary_busy'" in first_payload.decode("utf-8")
        first_state = _json(fixture.state_url(target_id))
        assert first_state["attempt_count"] == 1
        assert first_state["effect_count"] == 0

        second_status, _, second_payload, _ = _request(
            f"{fixture.base_url}/actions/{target_id}",
            method="POST",
        )
        assert second_status == 200
        assert "data-state='succeeded'" in second_payload.decode("utf-8")
        second_state = _json(fixture.state_url(target_id))
        assert second_state["attempt_count"] == 2
        assert second_state["effect_count"] == 1


def test_rate_limit_like_transient_has_retry_after_and_no_first_effect() -> None:
    with ScenarioBFixtureServer() as fixture:
        target_id = "scenario-b-07"
        first_status, first_headers, first_payload, _ = _request(
            f"{fixture.base_url}/actions/{target_id}",
            method="POST",
        )
        assert first_status == 429
        assert first_headers["Retry-After"] == "1"
        assert "data-state='rate_limited'" in first_payload.decode("utf-8")
        assert _json(fixture.state_url(target_id))["effect_count"] == 0

        second_status, _, _, _ = _request(
            f"{fixture.base_url}/actions/{target_id}",
            method="POST",
        )
        assert second_status == 200
        state = _json(fixture.state_url(target_id))
        assert state["attempt_count"] == 2
        assert state["effect_count"] == 1


def test_deterministic_failure_never_records_external_effect() -> None:
    with ScenarioBFixtureServer() as fixture:
        target_id = "scenario-b-09"
        for _ in range(2):
            status, _, payload, _ = _request(
                f"{fixture.base_url}/actions/{target_id}",
                method="POST",
            )
            assert status == 422
            assert "data-state='failed'" in payload.decode("utf-8")
        state = _json(fixture.state_url(target_id))
        assert state["attempt_count"] == 2
        assert state["effect_count"] == 0


def test_delayed_control_and_delayed_result_are_explicit_semantic_states() -> None:
    with ScenarioBFixtureServer() as fixture:
        delayed_control = _request(fixture.target_url("scenario-b-03"))[2].decode("utf-8")
        assert "role='region' aria-label='Action area'" in delayed_control
        assert "setTimeout" in delayed_control
        assert "aria-label='Execute target'" in delayed_control

        status, _, payload, _ = _request(
            f"{fixture.base_url}/actions/scenario-b-11",
            method="POST",
        )
        assert status == 200
        delayed_result = payload.decode("utf-8")
        assert "aria-label='Action result'" in delayed_result
        assert "data-state='pending'" in delayed_result
        assert "Delayed result confirmed" in delayed_result
        state = _json(fixture.state_url("scenario-b-11"))
        assert state["effect_count"] == 1


def test_duplicate_accessible_name_is_deliberately_ambiguous() -> None:
    with ScenarioBFixtureServer() as fixture:
        html = _request(fixture.target_url("scenario-b-13"))[2].decode("utf-8")
        parser = _SemanticParser()
        parser.feed(html)
        execute_buttons = [
            attrs
            for tag, attrs in parser.tags
            if tag == "button" and attrs.get("aria-label") == "Execute target"
        ]
        assert len(execute_buttons) == 2
        assert _json(fixture.state_url("scenario-b-13"))["attempt_count"] == 0


def test_disabled_then_enabled_uses_named_control_without_selector_identity_contract() -> None:
    with ScenarioBFixtureServer() as fixture:
        html = _request(fixture.target_url("scenario-b-15"))[2].decode("utf-8")
        parser = _SemanticParser()
        parser.feed(html)
        execute_buttons = [
            attrs
            for tag, attrs in parser.tags
            if tag == "button" and attrs.get("aria-label") == "Execute target"
        ]
        assert len(execute_buttons) == 1
        assert "disabled" in execute_buttons[0]
        assert "button.disabled=false" in html
        assert "Control enabled" in html


def test_navigation_result_has_distinct_url_and_semantic_success_evidence() -> None:
    with ScenarioBFixtureServer() as fixture:
        target_id = "scenario-b-17"
        status, _, payload, final_url = _request(
            f"{fixture.base_url}/actions/{target_id}",
            method="POST",
        )
        assert status == 200
        assert final_url == f"{fixture.base_url}/results/{target_id}"
        html = payload.decode("utf-8")
        assert "aria-label='Action result'" in html
        assert "Navigation result confirmed" in html
        state = _json(fixture.state_url(target_id))
        assert state["attempt_count"] == 1
        assert state["effect_count"] == 1


def test_ambiguous_post_action_state_is_explicitly_not_retry_safe() -> None:
    with ScenarioBFixtureServer() as fixture:
        target_id = "scenario-b-19"
        status, _, payload, _ = _request(
            f"{fixture.base_url}/actions/{target_id}",
            method="POST",
        )
        assert status == 202
        html = payload.decode("utf-8")
        assert "data-state='ambiguous'" in html
        assert "data-retry-safe='false'" in html
        assert "automatic retry forbidden" in html
        state = _json(fixture.state_url(target_id))
        assert state == {
            "target_id": target_id,
            "family": "ambiguous_action_no_retry",
            "attempt_count": 1,
            "effect_count": 1,
            "retry_safe": False,
        }


def test_state_survives_browser_client_restart_and_reset_is_deterministic() -> None:
    with ScenarioBFixtureServer() as fixture:
        target_id = "scenario-b-20"
        _request(f"{fixture.base_url}/actions/{target_id}", method="POST")

        first_client_state = _json(fixture.state_url(target_id))
        second_client_state = _json(fixture.state_url(target_id))
        assert second_client_state == first_client_state
        assert second_client_state["effect_count"] == 1

        fixture.reset(target_id)
        assert _json(fixture.state_url(target_id))["attempt_count"] == 0
        assert _json(fixture.state_url(target_id))["effect_count"] == 0
