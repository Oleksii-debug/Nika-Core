from __future__ import annotations

from pathlib import Path

from nika_core.media.contracts import ComponentState, OptionalComponent


class OptionalComponentRegistry:
    """In-memory view of optional media capability state.

    Persistence of durable job decisions remains in MediaRepository. This registry deliberately
    does not download or install anything; callers must perform acquisition through an explicit
    product action and then update the observed state.
    """

    def __init__(self) -> None:
        self._components: dict[str, OptionalComponent] = {}

    def set(self, component: OptionalComponent) -> None:
        self._components[component.component_id] = component

    def get(self, component_id: str) -> OptionalComponent:
        return self._components.get(
            component_id,
            OptionalComponent(
                component_id=component_id,
                state=ComponentState.MISSING,
                message="Optional component is not installed or has not been discovered.",
            ),
        )

    def all(self) -> tuple[OptionalComponent, ...]:
        return tuple(self._components[key] for key in sorted(self._components))

    def discover_executable(self, component_id: str, path: Path | None) -> OptionalComponent:
        if path is None or not path.is_file():
            component = OptionalComponent(
                component_id=component_id,
                state=ComponentState.MISSING,
                message="Executable is missing. Nika will not download it automatically.",
            )
        else:
            component = OptionalComponent(
                component_id=component_id,
                state=ComponentState.AVAILABLE,
                path_hint=str(path),
                message="Executable discovered locally.",
            )
        self.set(component)
        return component
