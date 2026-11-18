# Plan 1: reproducible dependency installation

## Decision and scope

REUSE Astral uv 0.13.6 as a development-only dependency resolver; REUSE pip
hash-checking for CI installation. Nika does not implement another resolver,
package repository, runtime authority or license classifier. The official
references are [uv locking](https://docs.astral.sh/uv/pip/compile/),
[uv export](https://docs.astral.sh/uv/concepts/projects/export/) and
[pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/).

`pyproject.toml` and `PLAN1_DEPENDENCY_AUTHORITY.json` retain their roles as
declared ranges/adoption decisions. `uv.lock` separately pins the resolved
transitive graph for all declared extras and Python 3.12–3.13. Resolution is
not evidence that every optional platform/engine has been installed or tested.
No model weights, browser profiles, keys or private index URLs are included.

## Actual CI and release installation

| Export | Existing consumer | Selected extras |
|---|---|---|
| `requirements/core.txt` | Core Ubuntu and Windows verification | dev, agent, planning |
| `requirements/m12-ubuntu.txt` | M12 Ubuntu/browser proof | dev, agent, planning, browser |
| `requirements/m12-windows.txt` | M12 Windows/UIA proof | dev, agent, planning, windows-interaction |
| `requirements/packaging.txt` | M11 and M12 Windows package | gui, qa, dev |
| `requirements/build.txt` | All four profiles, before project build | exact setuptools/wheel and their dependencies |

These are universal pip exports, including platform/Python markers and
distribution SHA-256 hashes. CI verifies their source-bound manifest before
installation, then uses pip `--require-hashes`. Source distributions build with
`--no-build-isolation` using the separately installed, pinned build environment;
the editable Nika source is installed with `--no-deps --no-build-isolation` so it
cannot silently re-resolve dependencies. This is not a bit-reproducible native
compiler/OS image or a final executable attestation.

`scripts/plan1_dependency_environment.py --include-transitive --strict` traverses
the actual installed dependency graph with active platform/extra markers and
checks every incoming version constraint, including diamond dependencies.
It records exact versions, source commit, metadata-text hashes and available
license-text hashes, reusing the release notice collector. No unrelated
installed distributions, absolute environment paths or credential files are
included. Core/M11/M12 preserve the report as a commit-bound Actions artifact.

## Updating and verifying

With uv 0.13.6 available in a development environment:

```sh
python scripts/plan1_dependency_lock.py --refresh
python scripts/plan1_dependency_lock.py
python -m pip install --require-hashes -r requirements/build.txt
python -m pip install --require-hashes --no-build-isolation -r requirements/core.txt
python -m pip install --no-deps --no-build-isolation -e .
python scripts/plan1_dependency_environment.py --groups base agent planning dev build-system --include-transitive --strict --output plan1-dependencies.json
python scripts/verify.py
```

Refresh preserves uv's existing versions unless constraints require a change.
An intentional dependency upgrade must review the changed lock, exports,
upstream artifacts/licenses and applicable tests together. Git attributes keep
byte-bound inputs LF on Windows and Linux. Previous locks remain in Git history;
rollback restores the dependency inputs, lock, exports and manifest together.

## Remaining evidence boundaries

Metadata declarations and collected license-text hashes are evidence for a
version-specific audit, **not independent legal clearance**. Missing license
texts and restrictive/mixed-license/native bundles still require component-level
review under `THIRD_PARTY_ADOPTION.md`. In particular optional package resolution
does not authorize redistributing its engine or model assets.

Ubuntu/Windows Actions must qualify the exact combined candidate. A local
Linux install does not award Windows, packaged, physical NVDA, Web/Cloud or
whole-product acceptance. Plan 1 is not terminal DONE from this file alone.
