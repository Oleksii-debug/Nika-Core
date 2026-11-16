from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_ci_interpreter_excludes_runner_packages_and_refuses_reuse(tmp_path: Path) -> None:
    root = tmp_path / "Окрема перевірка"
    root.mkdir()
    github_path = root / "github-path"
    github_env = root / "github-env"
    environment = {
        **os.environ, "GITHUB_PATH": str(github_path), "GITHUB_ENV": str(github_env),
    }
    script = Path(__file__).resolve().parents[1] / "scripts" / "ci_dependency_environment.py"
    subprocess.run(
        [sys.executable, str(script)], cwd=root, env=environment, check=True, timeout=60,
    )
    binaries = Path(github_path.read_text(encoding="utf-8").strip())
    interpreter = binaries / ("python.exe" if os.name == "nt" else "python")
    child_environment = dict(environment)
    child_environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            str(interpreter), "-c", (
                "import importlib.util,json,site,sys; "
                "print(json.dumps([sys.prefix != sys.base_prefix,site.ENABLE_USER_SITE,"
                "importlib.util.find_spec('pytest') is None]))"
            ),
        ],
        cwd=root, env=child_environment, check=True, capture_output=True, text=True, timeout=30,
    )
    assert json.loads(result.stdout) == [True, False, True]
    subprocess.run(
        [str(interpreter), str(script), "--verify"], cwd=root,
        env=child_environment, check=True, timeout=30,
    )
    assert "PYTHONPATH=\n" in github_env.read_text(encoding="utf-8")
    assert "PYTHONNOUSERSITE=1" in github_env.read_text(encoding="utf-8")
    sentinel = root / ".ci-venv" / "preserve-existing-state"
    sentinel.write_text("preserve", encoding="utf-8")
    retry = subprocess.run(
        [sys.executable, str(script)], cwd=root, env=environment, check=False, timeout=30,
    )
    assert retry.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_locked_jobs_check_isolation_before_installing_dependencies() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for name in ("ci.yml", "m11-windows-release.yml", "m12-prehuman-release-gate.yml"):
        source = (workflows / name).read_text(encoding="utf-8")
        blocks = source.split("run: python scripts/ci_dependency_environment.py\n")[1:]
        assert blocks
        for block in blocks:
            assert block.index("ci_dependency_environment.py --verify") < block.index(
                "pip install --require-hashes"
            )
