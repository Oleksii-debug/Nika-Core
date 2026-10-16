from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Any

from nika_core.ui.bridge_models import UIResult
from nika_core.v01_model_settings import V01ModelSettings

_PREFIXES = ("intelligence mode", "режим інтелекту")
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_MAX_COMMAND_BYTES = 4096
_HELP = (
    "Команда режиму інтелекту має бути однією з форм: "
    "«режим інтелекту»; "
    "«режим інтелекту deterministic»; "
    "«режим інтелекту foundry <model>»; "
    "«режим інтелекту ollama <model> <loopback-url>»; "
    "«режим інтелекту api <provider> <model> <https-url> <env:REF> "
    "<public|private>»."
)


def _normalize_command(command: str) -> str:
    if type(command) is not str:
        raise TypeError("command must be exact text")
    if any(
        unicodedata.category(character) in _FORBIDDEN_CATEGORIES
        for character in command
    ):
        raise ValueError("command contains forbidden Unicode controls")
    try:
        encoded = command.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("command must be valid UTF-8") from exc
    if len(encoded) > _MAX_COMMAND_BYTES:
        raise ValueError("command is too large")
    return " ".join(command.split()).strip()


def is_packaged_intelligence_mode_command(command: str) -> bool:
    """Recognize the reserved namespace so malformed special commands fail closed."""

    if type(command) is not str:
        return False
    visible_probe = "".join(
        character
        for character in command
        if unicodedata.category(character) not in _FORBIDDEN_CATEGORIES
    )
    normalized = " ".join(visible_probe.split()).strip().casefold()
    return any(
        normalized == prefix or normalized.startswith(prefix + " ")
        for prefix in _PREFIXES
    )


class PackagedIntelligenceModeCommandAdapter:
    """Translate explicit command text into the canonical durable model-settings authority."""

    def __init__(self, settings: V01ModelSettings) -> None:
        if type(settings) is not V01ModelSettings:
            raise TypeError("settings must be the exact V01ModelSettings authority")
        self._settings = settings

    @staticmethod
    def _rejected(message: str = _HELP) -> UIResult:
        return UIResult(
            request_id="model-settings",
            status="rejected",
            message=message,
            focus_id="command-input",
        )

    def _status(self) -> UIResult:
        snapshot = self._settings.snapshot()
        status = snapshot.get("status")
        if status == "missing":
            return UIResult(
                request_id="model-settings",
                status="completed",
                message="Режим інтелекту для нових завдань ще не налаштовано.",
                focus_id="model-settings-heading",
            )
        if status != "ready":
            return UIResult(
                request_id="model-settings",
                status="failed",
                message="Не вдалося безпечно прочитати режим інтелекту.",
                focus_id="model-settings-heading",
            )
        mode = snapshot.get("intelligence_mode")
        route = snapshot.get("route_kind")
        provider = snapshot.get("provider_id")
        model = snapshot.get("model")
        if (
            type(mode) is not str
            or type(route) is not str
            or (provider is not None and type(provider) is not str)
            or (model is not None and type(model) is not str)
        ):
            return UIResult(
                request_id="model-settings",
                status="failed",
                message="Не вдалося безпечно прочитати режим інтелекту.",
                focus_id="model-settings-heading",
            )
        provider_text = provider if provider is not None else "немає"
        model_text = model if model is not None else "немає"
        return UIResult(
            request_id="model-settings",
            status="completed",
            message=(
                f"Режим інтелекту: {mode}; route {route}; "
                f"provider {provider_text}; model {model_text}."
            ),
            focus_id="model-settings-heading",
        )

    def _revision(self) -> int | None:
        snapshot = self._settings.snapshot()
        if snapshot.get("status") == "invalid":
            return None
        revision = snapshot.get("revision", 0)
        if type(revision) is not int or revision < 0:
            return None
        return revision

    def _configure(self, payload: Mapping[str, Any]) -> UIResult:
        revision = self._revision()
        if revision is None:
            return self._rejected(
                "Збережені налаштування моделі пошкоджені або несумісні."
            )
        result = self._settings.configure({**dict(payload), "revision": revision})
        if result.status == "completed":
            return result
        return UIResult(
            request_id=result.request_id,
            status=result.status,
            message=result.message,
            focus_id="command-input",
        )

    def execute(self, command: str) -> UIResult:
        try:
            normalized = _normalize_command(command)
        except (TypeError, ValueError):
            return self._rejected()

        lowered = normalized.casefold()
        prefix = next(
            (
                item
                for item in _PREFIXES
                if lowered == item or lowered.startswith(item + " ")
            ),
            None,
        )
        if prefix is None:
            return self._rejected()

        remainder = normalized[len(prefix) :].strip()
        if not remainder or remainder.casefold() in {"status", "стан"}:
            return self._status()

        parts = remainder.split(" ")
        mode = parts[0].casefold()
        common: dict[str, object] = {
            "schema_version": 1,
            "credential_ref": None,
            "private_data_allowed": True,
            "timeout_seconds": 60.0,
        }

        if mode in {"deterministic", "no_llm", "детермінований"} and len(parts) == 1:
            return self._configure(
                {
                    **common,
                    "route_kind": "deterministic",
                    "provider_id": None,
                    "model": None,
                    "base_url": None,
                }
            )

        if mode in {"foundry", "foundry_local", "embedded"} and len(parts) == 2:
            return self._configure(
                {
                    **common,
                    "route_kind": "foundry_local",
                    "provider_id": "foundry-local",
                    "model": parts[1],
                    "base_url": None,
                }
            )

        if mode in {"ollama", "local_external"} and len(parts) == 3:
            return self._configure(
                {
                    **common,
                    "route_kind": "ollama",
                    "provider_id": "ollama",
                    "model": parts[1],
                    "base_url": parts[2],
                }
            )

        if mode in {"api", "openai_compatible", "api_configured"} and len(parts) == 6:
            privacy = parts[5].casefold()
            if privacy not in {"public", "private"}:
                return self._rejected()
            return self._configure(
                {
                    **common,
                    "route_kind": "openai_compatible",
                    "provider_id": parts[1],
                    "model": parts[2],
                    "base_url": parts[3],
                    "credential_ref": parts[4],
                    "private_data_allowed": privacy == "private",
                }
            )

        return self._rejected()
