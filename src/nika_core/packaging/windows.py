from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


_WINDOWS_INVALID_NAME_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAME_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
    | {"COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}
)


def _require_windows_bundle_name(name: str) -> str:
    if type(name) is not str:
        raise TypeError("Windows bundle name must be exact text")
    if not name or name != name.strip():
        raise ValueError("Windows bundle name must be non-empty canonical text")
    try:
        utf16_units = len(name.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ValueError("Windows bundle name must be valid Unicode") from exc
    if utf16_units > 255:
        raise ValueError(
            "Windows bundle name exceeds the 255 UTF-16 code-unit component limit"
        )
    if any(
        ord(character) < 32
        or ord(character) == 127
        or character in _WINDOWS_INVALID_NAME_CHARS
        for character in name
    ):
        raise ValueError(
            "Windows bundle name contains an invalid Windows filename character"
        )
    if name.endswith((".", " ")):
        raise ValueError("Windows bundle name may not end with a dot or space")
    stem = name.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAME_STEMS:
        raise ValueError("Windows bundle name uses a reserved Windows device name")
    return name


def _reject_linked_path(label: str, path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink() or component.is_junction():
            raise ValueError(
                f"{label} path traverses a symbolic link or junction: {component}"
            )


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
        _require_windows_bundle_name(self.name)
        for label, path in (("entrypoint", self.entrypoint), ("web_assets", self.web_assets)):
            # Canonical #1051/#1052 source-input authority: linked ancestors
            # can redirect ordinary leaf paths outside the exact checkout.
            # Inspect links before exists()/is_file()/is_dir(), which dereference
            # the path and could otherwise touch redirected or broken targets.
            _reject_linked_path(label, path)
            if not path.exists():
                raise FileNotFoundError(f"{label} does not exist: {path}")
        for label, path in (
            ("dist_dir", self.dist_dir),
            ("work_dir", self.work_dir),
            ("spec_dir", self.spec_dir),
        ):
            _reject_linked_path(label, path)
        # A pre-existing bundle leaf can itself be a junction/symlink even when
        # dist_dir is ordinary. Reject it before PyInstaller is allowed to
        # delete/write output through --noconfirm.
        _reject_linked_path(
            "bundle_dir",
            self.dist_dir / _require_windows_bundle_name(self.name),
        )
        if not self.entrypoint.is_file():
            raise ValueError("entrypoint must be a file")
        if not self.web_assets.is_dir():
            raise ValueError("web_assets must be a directory")
        # Check every entry before descending so a nested link/junction cannot
        # import bytes from outside the intended accessible UI payload.
        pending = [self.web_assets]
        while pending:
            directory = pending.pop()
            for asset in directory.iterdir():
                if asset.is_symlink() or asset.is_junction():
                    relative = asset.relative_to(self.web_assets)
                    raise ValueError(
                        "web_assets contains a symbolic link or junction: "
                        f"{relative}"
                    )
                if asset.is_dir():
                    pending.append(asset)
        # The installed WebView2 journey depends on all three source assets.
        for required in ("index.html", "app.js", "styles.css"):
            asset = self.web_assets / required
            if not asset.is_file() or asset.stat().st_size == 0:
                raise ValueError(f"web_assets must contain a non-empty {required}")

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
        _reject_linked_path("dist_dir", self.dist_dir)
        bundle = self.dist_dir / _require_windows_bundle_name(self.name)
        # Revalidate the leaf after the build as well. Release evidence,
        # notices and manifests must never follow a swapped output junction.
        _reject_linked_path("bundle_dir", bundle)
        return bundle


def default_windows_plan(project_root: Path) -> WindowsBuildPlan:
    _reject_linked_path("project_root", project_root)
    root = project_root.resolve()
    build_root = root / "build" / "m11"
    return WindowsBuildPlan(
        entrypoint=root / "scripts" / "nika_windows.py",
        web_assets=root / "src" / "nika_core" / "ui" / "web",
        dist_dir=root / "dist",
        work_dir=build_root / "work",
        spec_dir=build_root / "spec",
    )
