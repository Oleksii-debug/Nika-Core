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
    def __init__(self) -> None: super().__init__(); self.tags: list[tuple[str, dict[str, str | None]]] = []
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None: self.tags.append((tag, dict(attrs)))

def _request(url: str, *, method: str = "GET") -> tuple[int, dict[str, str], bytes, str]:
    request=urllib.request.Request(url,method=method)
    try:
        with urllib.request.urlopen(request,timeout=5) as response: return response.status,dict(response.headers.items()),response.read(),response.geturl()
    except urllib.error.HTTPError as exc: return exc.code,dict(exc.headers.items()),exc.read(),exc.geturl()

def _json(url:str)->dict[str,object]:
    status,_,payload,_=_request(url); assert status==200; parsed=json.loads(payload.decode()); assert isinstance(parsed,dict); return parsed

def test_manifest_has_exactly_20_stable_targets_and_two_per_family()->None:
    manifest=scenario_b_manifest(); assert len(manifest)==20; assert [x["input_order"] for x in manifest]==list(range(1,21)); assert [x["target_id"] for x in manifest]==[f"scenario-b-{i:02d}" for i in range(1,21)]; assert Counter(x["family"] for x in manifest)==Counter({f.value:2 for f in FixtureFamily}); assert [x["retry_safe"] for x in manifest[-2:]]==[False,False]

def test_loopback_server_exposes_manifest_semantics_and_bounded_fault_families()->None:
    with ScenarioBFixtureServer() as fixture:
        health=_json(fixture.url("/healthz")); assert health=={"fixture":"scenario-b","status":"ok","target_count":20}
        remote=_json(fixture.url("/manifest.json")); assert remote["scenario"]=="B"; assert remote["targets"]==list(scenario_b_manifest())
        for target in SCENARIO_B_TARGETS:
            status,headers,payload,_=_request(fixture.url(target.path)); assert status==200; assert headers["Cache-Control"]=="no-store"; assert "default-src 'self'" in headers["Content-Security-Policy"]
            parser=_SemanticParser(); parser.feed(payload.decode()); attrs=[a for tag,a in parser.tags if tag=="main"]; assert len(attrs)==1; assert attrs[0]["role"]=="main"; assert attrs[0]["aria-label"]==f"Scenario B target {target.target_id}"
            if target.family is FixtureFamily.DUPLICATE_ACCESSIBLE_NAME: assert sum(1 for tag,a in parser.tags if tag=="button" and a.get("aria-label")=="Execute target")==2
            if target.family is FixtureFamily.DISABLED_THEN_ENABLED: assert any(tag=="button" and "disabled" in a for tag,a in parser.tags)

def test_transient_faults_are_retryable_and_effect_occurs_once()->None:
    with ScenarioBFixtureServer() as fixture:
        for target_id,first_status in (("scenario-b-05",503),("scenario-b-07",429)):
            status,headers,_,_=_request(fixture.url(f"/actions/{target_id}"),method="POST"); assert status==first_status
            if first_status==429: assert headers["Retry-After"]=="1"
            status,_,payload,_=_request(fixture.url(f"/actions/{target_id}"),method="POST"); assert status==200; assert "Success" in payload.decode()
            state=_json(fixture.url(f"/state/{target_id}")); assert state["attempt_count"]==2; assert state["effect_count"]==1; assert state["retry_safe"] is True

def test_deterministic_failure_never_records_effect()->None:
    with ScenarioBFixtureServer() as fixture:
        status,_,payload,_=_request(fixture.url("/actions/scenario-b-09"),method="POST"); assert status==422; assert "Deterministic validation failure" in payload.decode()
        state=_json(fixture.url("/state/scenario-b-09")); assert state["attempt_count"]==1; assert state["effect_count"]==0

def test_navigation_result_is_observable_and_effect_counted_once()->None:
    with ScenarioBFixtureServer() as fixture:
        status,_,_,location=_request(fixture.url("/actions/scenario-b-17"),method="POST"); assert status==200; assert location.endswith("/results/scenario-b-17"); assert "Navigation result confirmed" in _request(location)[2].decode()
        state=_json(fixture.url("/state/scenario-b-17")); assert state["attempt_count"]==1; assert state["effect_count"]==1

def test_ambiguous_action_family_declares_no_retry_and_never_records_effect()->None:
    with ScenarioBFixtureServer() as fixture:
        status,_,payload,_=_request(fixture.url("/actions/scenario-b-19"),method="POST"); assert status==409; assert "Ambiguous action outcome" in payload.decode(); state=_json(fixture.url("/state/scenario-b-19")); assert state["attempt_count"]==1; assert state["effect_count"]==0; assert state["retry_safe"] is False
