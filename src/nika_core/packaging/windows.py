from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WindowsBuildPlan:
    """Deterministic PyInstaller arguments for the Windows desktop release candidate."""

    entrypoint: Path
    web_assets: Path
    dist_dir: Path
    work_dir: Path
    spec_dir: Path
    name: str = "NikaCore"
    windowed: bool = True
    clean: bool = True

    def validate(self) -> None:
        for label, path in (("entrypoint", self.entrypoint), ("web_assets", self.web_assets)):
            if not path.exists():
                raise FileNotFoundError(f"{label} does not exist: {path}")
        if not self.entrypoint.is_file():
            raise ValueError("entrypoint must be a file")
        if not self.web_assets.is_dir():
            raise ValueError("web_assets must be a directory")

    def pyinstaller_args(self) -> tuple[str, ...]:
        self.validate()
        args = [
            str(self.entrypoint),
            "--name",
            self.name,
            "--onedir",
            "--noconfirm",
            "--distpath",
            str(self.dist_dir),
            "--workpath",
            str(self.work_dir),
            "--specpath",
            str(self.spec_dir),
            "--add-data",
            f"{self.web_assets}:nika_core/ui/web",
        ]
        if self.windowed:
            args.append("--windowed")
        if self.clean:
            args.append("--clean")
        return tuple(args)

    @property
    def bundle_dir(self) -> Path:
        return self.dist_dir / self.name


def default_windows_plan(project_root: Path) -> WindowsBuildPlan:
    root = project_root.resolve()
    build_root = root / "build" / "m11"
    return WindowsBuildPlan(
        entrypoint=root / "src" / "nika_core" / "__main__.py",
        web_assets=root / "src" / "nika_core" / "ui" / "web",
        dist_dir=root / "dist",
        work_dir=build_root / "work",
        spec_dir=build_root / "spec",
    )
