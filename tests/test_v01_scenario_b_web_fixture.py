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

def test_server_is_loopback_only_and_manifest_contains_relative_target_paths()->None:
    with ScenarioBFixtureServer() as fixture:
        assert fixture.base_url.startswith("http://127.0.0.1:"); manifest=_json(f"{fixture.base_url}/manifest.json"); assert manifest["scenario"]=="B"; targets=manifest["targets"]; assert isinstance(targets,list) and len(targets)==20
        for item in targets: assert isinstance(item,dict); assert str(item["path"]).startswith("/targets/"); assert "://" not in str(item["path"])

def test_every_target_page_has_named_semantics()->None:
    with ScenarioBFixtureServer() as fixture:
        for target in SCENARIO_B_TARGETS:
            status,_,payload,_=_request(fixture.target_url(target.target_id)); assert status==200; html=payload.decode(); assert "http://" not in html and "https://" not in html
            parser=_SemanticParser(); parser.feed(html); mains=[a for t,a in parser.tags if t=="main"]; statuses=[a for _,a in parser.tags if a.get("role")=="status" and a.get("aria-label")=="Target status"]
            assert mains==[{"role":"main","aria-label":f"Scenario B target {target.target_id}"}]; assert len(statuses)==1 and statuses[0]["data-target-id"]==target.target_id

def test_effect_and_retry_families()->None:
    with ScenarioBFixtureServer() as fixture:
        s,_,p,_=_request(f"{fixture.base_url}/actions/scenario-b-01",method="POST"); assert s==200 and "data-state='succeeded'" in p.decode(); assert _json(fixture.state_url("scenario-b-01"))["effect_count"]==1
        s,_,_,_=_request(f"{fixture.base_url}/actions/scenario-b-05",method="POST"); assert s==503 and _json(fixture.state_url("scenario-b-05"))["effect_count"]==0
        s,_,_,_=_request(f"{fixture.base_url}/actions/scenario-b-05",method="POST"); assert s==200 and _json(fixture.state_url("scenario-b-05"))["effect_count"]==1
        s,h,_,_=_request(f"{fixture.base_url}/actions/scenario-b-07",method="POST"); assert s==429 and h["Retry-After"]=="1" and _json(fixture.state_url("scenario-b-07"))["effect_count"]==0

def test_failure_ambiguity_navigation_and_reset()->None:
    with ScenarioBFixtureServer() as fixture:
        s,_,_,_=_request(f"{fixture.base_url}/actions/scenario-b-09",method="POST"); assert s==422 and _json(fixture.state_url("scenario-b-09"))["effect_count"]==0
        s,_,p,_=_request(f"{fixture.base_url}/actions/scenario-b-19",method="POST"); assert s==202 and "data-state='ambiguous'" in p.decode() and "data-retry-safe='false'" in p.decode(); assert _json(fixture.state_url("scenario-b-19"))["effect_count"]==1
        s,_,p,u=_request(f"{fixture.base_url}/actions/scenario-b-17",method="POST"); assert s==200 and u.endswith("/results/scenario-b-17") and "Navigation result confirmed" in p.decode()
        fixture.reset("scenario-b-19"); assert _json(fixture.state_url("scenario-b-19"))["attempt_count"]==0
