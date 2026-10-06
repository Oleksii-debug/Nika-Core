from __future__ import annotations

import logging
from dataclasses import dataclass

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_packaged_build_continuation import (
    PackagedReviewedBuildContinuation,
)
from nika_core.product_factory_packaged_build_settings import (
    ActivatedPackagedBuildRuntime,
    PackagedBuildRuntimeSettings,
    PackagedBuildRuntimeSettingsError,
    activate_packaged_build_runtime,
    decode_packaged_build_runtime_config,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.product_factory_packaged_preparation import PreparedProductFactory

_BUILD_RUNTIME_FOCUS = "product-factory-build-authority-json"


class PackagedBuildRuntimeSessionError(RuntimeError):
    """Fail-closed packaged PF5 launch/effect-boundary session failure."""


@dataclass(slots=True)
class PackagedBuildRuntimeSession:
    """Launch-frozen PF5 composition above the durable settings authority."""

    store: SQLiteStore
    settings: PackagedBuildRuntimeSettings
    startup: PackagedLocalProductFactoryStartup | None
    product_factory_active: bool
    launch_revision: int
    launch_config_json: str | None
    activation: ActivatedPackagedBuildRuntime | None
    launch_invalid: bool = False
    activation_failed: bool = False

    def __post_init__(self) -> None:
        if type(self.store) is not SQLiteStore:
            raise TypeError("PF5 session store must be exact SQLiteStore")
        if type(self.settings) is not PackagedBuildRuntimeSettings:
            raise TypeError("PF5 session settings must be exact PackagedBuildRuntimeSettings")
        if (
            self.startup is not None
            and type(self.startup) is not PackagedLocalProductFactoryStartup
        ):
            raise TypeError("PF5 session startup authority is invalid")
        if type(self.product_factory_active) is not bool:
            raise TypeError("PF5 session product_factory_active must be exact bool")
        if type(self.launch_revision) is not int or self.launch_revision < 0:
            raise TypeError("PF5 session launch revision must be a non-negative integer")
        if self.launch_config_json is not None and type(self.launch_config_json) is not str:
            raise TypeError("PF5 session launch config must be text or None")
        if (
            self.activation is not None
            and type(self.activation) is not ActivatedPackagedBuildRuntime
        ):
            raise TypeError("PF5 session activation must be exact ActivatedPackagedBuildRuntime")
        if type(self.launch_invalid) is not bool or type(self.activation_failed) is not bool:
            raise TypeError("PF5 session failure flags must be exact bool")

    def snapshot(self) -> dict[str, object]:
        current = self._raw_snapshot()
        result = dict(current)
        result["runtime_status"] = self._runtime_status(current)
        return result

    def execution_focus(self) -> str | None:
        current = self._raw_snapshot()
        status = self._runtime_status(current)
        if status == "invalid":
            return _BUILD_RUNTIME_FOCUS
        configured_now = current.get("configured") is True
        if (
            status in {"restart_required", "product_factory_required"}
            and (configured_now or self.launch_config_json is not None)
        ):
            return _BUILD_RUNTIME_FOCUS
        return None

    @property
    def post_dispatch_enabled(self) -> bool:
        return self.activation is not None and self.startup is not None

    async def post_dispatch(self, prepared: PreparedProductFactory) -> None:
        if not self.post_dispatch_enabled:
            raise PackagedBuildRuntimeSessionError(
                "packaged PF5 continuation is not active for this launch"
            )
        if self._runtime_status(self._raw_snapshot()) != "active":
            raise PackagedBuildRuntimeSessionError(
                "packaged PF5 settings changed after launch; restart is required"
            )
        assert self.activation is not None
        assert self.startup is not None
        continuation = PackagedReviewedBuildContinuation(
            self.store,
            self.startup,
            self.activation,
            effect_admission_guard=self._continuation_effect_allowed,
        )
        await continuation(prepared)

    def _continuation_effect_allowed(self) -> bool:
        return self._runtime_status(self._raw_snapshot()) == "active"

    def _raw_snapshot(self) -> dict[str, object]:
        return self.settings.snapshot(runtime_status="not_configured")

    def _runtime_status(self, current: dict[str, object]) -> str:
        current_revision = current.get("revision")
        current_json = current.get("config_json")
        if current.get("status") != "ready":
            return "invalid"
        if (
            current_revision != self.launch_revision
            or current_json != self.launch_config_json
        ):
            return "restart_required"
        if self.launch_invalid or self.activation_failed:
            return "invalid"
        if self.launch_config_json is None:
            return "not_configured"
        if not self.product_factory_active or self.startup is None:
            return "product_factory_required"
        if self.activation is None:
            return "invalid"
        return "active"


def build_packaged_build_runtime_session(
    store: SQLiteStore,
    *,
    settings: PackagedBuildRuntimeSettings,
    startup: PackagedLocalProductFactoryStartup | None,
    product_factory_active: bool,
) -> PackagedBuildRuntimeSession:
    """Freeze one PF5 settings generation and activate it only over active packaged PF4."""

    if type(store) is not SQLiteStore:
        raise TypeError("PF5 session store must be exact SQLiteStore")
    if type(settings) is not PackagedBuildRuntimeSettings:
        raise TypeError("PF5 session settings must be exact PackagedBuildRuntimeSettings")
    if startup is not None and type(startup) is not PackagedLocalProductFactoryStartup:
        raise TypeError("PF5 session startup authority is invalid")
    if type(product_factory_active) is not bool:
        raise TypeError("product_factory_active must be exact bool")

    initial = settings.snapshot(runtime_status="not_configured")
    raw_revision = initial.get("revision")
    launch_revision = raw_revision if type(raw_revision) is int and raw_revision >= 0 else 0
    raw_json = initial.get("config_json")
    launch_json = raw_json if type(raw_json) is str else None
    launch_invalid = initial.get("status") != "ready"
    activation: ActivatedPackagedBuildRuntime | None = None
    activation_failed = False

    if (
        not launch_invalid
        and launch_json is not None
        and product_factory_active
        and startup is not None
    ):
        try:
            config = decode_packaged_build_runtime_config(launch_json)
            activation = activate_packaged_build_runtime(
                store,
                startup=startup,
                config=config,
            )
        except PackagedBuildRuntimeSettingsError as exc:
            logging.getLogger(__name__).error(
                "Packaged PF5 activation failed: exception_type=%s",
                type(exc).__name__,
            )
            activation_failed = True
        except Exception as exc:  # noqa: BLE001 - packaged startup must fail closed
            logging.getLogger(__name__).error(
                "Packaged PF5 activation failed unexpectedly: exception_type=%s",
                type(exc).__name__,
            )
            activation_failed = True

        after = settings.snapshot(runtime_status="not_configured")
        if (
            after.get("status") != "ready"
            or after.get("revision") != launch_revision
            or after.get("config_json") != launch_json
        ):
            activation = None
            activation_failed = False

    return PackagedBuildRuntimeSession(
        store=store,
        settings=settings,
        startup=startup,
        product_factory_active=product_factory_active,
        launch_revision=launch_revision,
        launch_config_json=launch_json,
        activation=activation,
        launch_invalid=launch_invalid,
        activation_failed=activation_failed,
    )
