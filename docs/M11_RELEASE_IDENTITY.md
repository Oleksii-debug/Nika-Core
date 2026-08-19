# M11 release identity and version-source rules

Status: release-engineering control for Windows package candidates.

## Canonical version

The `[project].version` value in `pyproject.toml` is the single source of truth for the Nika Core application version. Release workflows derive ZIP names, artifact names and packaged window-title expectations from that value. `scripts/m11_release.py --version` is only an optional assertion and fails closed if it disagrees with `pyproject.toml`.

Changing `pyproject.toml` can change runtime dependencies and release notices even when no packaging source file changes, so it is an explicit M11 Windows release workflow trigger.

## Exact source identity

Every newly generated `release-manifest.json` uses manifest schema version 2 and records the exact 40-character source commit SHA in `source_sha` in addition to product/version and per-file size/SHA-256 evidence.

Release generation fails closed when an exact source SHA is unavailable. CI passes the pull-request head SHA (or the workflow SHA for an explicit dispatch) to the build. This prevents a distributable directory from being treated as an identified candidate merely because its version string and file hashes exist.

The M11 artifact name also includes the exact source SHA. M12 pre-human evidence schema version 3 records both the canonical product version and that the release manifest is source-SHA-bound.

## Candidate truth

A package built from an open feature/repair branch is evidence for that exact branch head only. It is not a fresh integrated human candidate after another source change merges into `main`.

A candidate may advance to human Windows/NVDA acceptance only after:

1. all required product/reliability changes are integrated on one exact `main` head;
2. the full applicable M12 gate succeeds on that exact integrated source state;
3. the generated package, `release-manifest.json`, notices and M12 evidence all agree on product version and source identity;
4. no later product/package-input change supersedes that artifact.

`HUMAN_TESTED` and `NVDA_VERIFIED` remain human-only evidence states.
