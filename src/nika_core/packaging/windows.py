from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WindowsBuildPlan:
    """Deterministic PyInstaller build arguments for the Windows desktop candidate."""

    entrypoint: Path
    name: str = "NikaCore"
    onefile: bool = False
    windowed: bool = True
    clean: bool = True
    web_assets: Path | None = None

    def pyinstaller_args(self) -> tuple[str, ...]:
        args: list[str] = [str(self.entrypoint), "--name", self.name]
        args.append("--onefile" if self.onefile else "--onedir")
        if self.windowed:
            args.append("--windowed")
        if self.clean:
            args.append("--clean")
        if self.web_assets is not None:
            args.extend(("--add-data", f"{self.web_assets};nika_core/ui/web"))
        return tuple(args)


def default_windows_plan(project_root: Path) -> WindowsBuildPlan:
    root = project_root.resolve()
    return WindowsBuildPlan(
        entrypoint=root / "src" / "nika_core" / "__main__.py",
        web_assets=root / "src" / "nika_core" / "ui" / "web",
    )
