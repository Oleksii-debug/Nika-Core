from __future__ import annotations

from collections.abc import Callable

from nika_core.diagnostics import ModelHealthFact, OllamaModelHealthProbe
from nika_core.v01_model_settings import ModelSelection


class _ExplosiveString(str):
    def strip(self, chars: str | None = None) -> str:
        raise AssertionError("hostile strip must not execute")

    def rstrip(self, chars: str | None = None) -> str:
        raise AssertionError("hostile rstrip must not execute")


class _CatalogSpoof(str):
    def __eq__(self, other: object) -> bool:
        return other == str.__str__(self) or other == "local-model:1"

    def __hash__(self) -> int:
        return hash("local-model:1")


class _Evidence:
    def __init__(self) -> None:
        self.calls = 0

    def has_successful_inference(
        self,
        *,
        provider_id: str,
        model_id: str,
        route_identity: str,
    ) -> bool:
        self.calls += 1
        raise AssertionError("invalid identity must not query inference evidence")


def _unexpected_client(**kwargs: object) -> None:
    raise AssertionError("invalid identity must not construct an HTTP client")


def _assert_rejected(
    *,
    model_id: str = "local-model:1",
    base_url: str = "http://localhost:11434",
    provider_id: str = "ollama",
) -> None:
    evidence = _Evidence()
    probe = OllamaModelHealthProbe(
        model_id=model_id,
        base_url=base_url,
        provider_id=provider_id,
        evidence_port=evidence,
        client_factory=_unexpected_client,
    )

    snapshot = probe.snapshot()

    assert snapshot.configured is ModelHealthFact.NO
    assert snapshot.reachable is ModelHealthFact.UNKNOWN
    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert snapshot.inference_proven is ModelHealthFact.UNKNOWN
    assert evidence.calls == 0


def test_string_subclass_identities_fail_before_overloadable_operations_or_effects() -> None:
    _assert_rejected(model_id=_ExplosiveString("local-model:1"))
    _assert_rejected(provider_id=_ExplosiveString("ollama"))
    _assert_rejected(base_url=_ExplosiveString("http://localhost:11434"))


def test_model_id_subclass_cannot_spoof_catalog_membership() -> None:
    _assert_rejected(model_id=_CatalogSpoof("attacker-model"))


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> object:
        return self._payload


class _CatalogClient:
    def __init__(self, payload: object, calls: list[str]) -> None:
        self._payload = payload
        self._calls = calls

    def __enter__(self) -> "_CatalogClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str) -> _Response:
        self._calls.append(url)
        if url.endswith("/api/tags"):
            return _Response(self._payload)
        return _Response({"models": []})


def _catalog_factory(
    payload: object,
    calls: list[str],
) -> Callable[..., _CatalogClient]:
    def create(**_kwargs: object) -> _CatalogClient:
        return _CatalogClient(payload, calls)

    return create


def test_route_text_rejects_noncanonical_endpoint_and_invisible_model_identities() -> None:
    _assert_rejected(base_url="http://localhost:11434//")
    _assert_rejected(base_url="http://localhost:0")
    for model_id in (
        "local-model:\u200b1",
        "local-model:\u20281",
        "local-model:\ud800",
        "local-model:\u00a01",
        "local-model:\ue0001",
        "m" * 513,
    ):
        _assert_rejected(model_id=model_id)


def test_single_trailing_slash_uses_canonical_metadata_endpoints() -> None:
    calls: list[str] = []

    def client_factory(**kwargs: object) -> _CatalogClient:
        assert kwargs == {
            "timeout": 2.0,
            "follow_redirects": False,
            "trust_env": False,
        }
        return _CatalogClient(
            {"models": [{"name": "local-model:1"}]},
            calls,
        )

    snapshot = OllamaModelHealthProbe(
        model_id="local-model:1",
        base_url="http://localhost:11434/",
        client_factory=client_factory,
    ).snapshot()

    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == [
        "http://localhost:11434/api/tags",
        "http://localhost:11434/api/ps",
    ]


def test_malformed_catalog_identity_is_unknown_not_absent() -> None:
    for catalog_model in (
        "local-model:1\u200b",
        " local-model:1",
        "m" * 513,
    ):
        calls: list[str] = []

        snapshot = OllamaModelHealthProbe(
            model_id="local-model:1",
            client_factory=_catalog_factory(
                {"models": [{"name": catalog_model}]},
                calls,
            ),
        ).snapshot()

        assert snapshot.model_present is ModelHealthFact.UNKNOWN
        assert snapshot.model_ready is ModelHealthFact.UNKNOWN
        assert calls == ["http://localhost:11434/api/tags"]


def test_oversized_catalog_fails_closed_without_runtime_inventory_probe() -> None:
    calls: list[str] = []
    payload = {"models": [{"name": "other-model:1"}] * 4097}

    snapshot = OllamaModelHealthProbe(
        model_id="local-model:1",
        client_factory=_catalog_factory(payload, calls),
    ).snapshot()

    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == ["http://localhost:11434/api/tags"]


class _RecursiveResponse(_Response):
    def __init__(self) -> None:
        super().__init__({})

    def json(self) -> object:
        raise RecursionError("deep untrusted Ollama JSON")


class _ResponseClient:
    def __init__(
        self,
        responses: dict[str, _Response],
        calls: list[str],
    ) -> None:
        self._responses = responses
        self._calls = calls

    def __enter__(self) -> "_ResponseClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str) -> _Response:
        self._calls.append(url)
        return self._responses[url]


def test_recursive_catalog_json_is_unknown_without_runtime_inventory_probe() -> None:
    base = "http://localhost:11434"
    calls: list[str] = []
    responses = {f"{base}/api/tags": _RecursiveResponse()}

    snapshot = OllamaModelHealthProbe(
        model_id="local-model:1",
        client_factory=lambda **_kwargs: _ResponseClient(responses, calls),
    ).snapshot()

    assert snapshot.model_present is ModelHealthFact.UNKNOWN
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == [f"{base}/api/tags"]


def test_recursive_running_json_preserves_known_presence() -> None:
    base = "http://localhost:11434"
    calls: list[str] = []
    responses = {
        f"{base}/api/tags": _Response({"models": [{"name": "local-model:1"}]}),
        f"{base}/api/ps": _RecursiveResponse(),
    }

    snapshot = OllamaModelHealthProbe(
        model_id="local-model:1",
        client_factory=lambda **_kwargs: _ResponseClient(responses, calls),
    ).snapshot()

    assert snapshot.model_present is ModelHealthFact.YES
    assert snapshot.model_ready is ModelHealthFact.UNKNOWN
    assert calls == [f"{base}/api/tags", f"{base}/api/ps"]

def test_model_selection_and_health_share_valid_ollama_url_contract() -> None:
    for base_url in (
        "http://localhost:11434",
        "http://localhost:11434/",
        "https://127.0.0.1:11434",
        "http://[::1]:11434",
        "https://[::1]:11434/",
    ):
        selection = ModelSelection(
            route_kind="ollama",
            provider_id="ollama",
            model="local-model:1",
            base_url=base_url,
            private_data_allowed=True,
            timeout_seconds=60,
        )
        calls: list[str] = []
        snapshot = OllamaModelHealthProbe(
            model_id=selection.model,
            base_url=selection.base_url,
            provider_id=selection.provider_id,
            client_factory=_catalog_factory({"models": []}, calls),
        ).snapshot()

        assert snapshot.configured is ModelHealthFact.YES
        assert snapshot.reachable is ModelHealthFact.YES
        assert snapshot.model_present is ModelHealthFact.NO
        assert calls == [f"{base_url.rstrip('/')}/api/tags"]

