# Deterministic Brain planning dependency evidence

Evidence date: 2026-08-24.
Scope: optional model-free formal-planning adapter only.

## Adopted surface

The Nika base install does not include a planning engine. The optional `planning` extra is intentionally exact:

- `unified-planning==1.3.0`;
- `up-aries==0.5.0`.

No Pyperplan package is part of this adopted extra. Nika domain contracts remain framework-neutral and the planner still reaches effects only through ordinary Nika tool/policy boundaries.

## Unified Planning 1.3.0

Decision: **ADAPT** behind Nika deterministic-planner contracts.

Authoritative publication evidence checked on 2026-08-23:

- PyPI project/version: `unified-planning` 1.3.0;
- declared license: Apache-2.0 / Apache Software License;
- source distribution: `unified_planning-1.3.0.tar.gz`;
- source-distribution SHA-256: `9f1914377172626e512bd4c1545aebddbfd4752bf37b36f9cd41ff059a7e6a52`;
- wheel SHA-256: `3ad9c790d238a12ce1dfd25f564e728d98350a0b4b79d3beb60978f95487fa46`;
- PyPI trusted-publishing attestation binds the release to source repository `aiplan4eu/unified-planning` commit `42e66926e400ab1367b5b02af504d8c7016b9243`.

This is the exact wrapper release Nika currently adopts; a future 1.x release receives no automatic adoption credit merely because it satisfies a version range.

## Aries 0.5.0

Decision: **ADAPT** as the optional first proof engine selected through Unified Planning.

Authoritative publication/upstream evidence checked on 2026-08-23:

- PyPI project/version: `up-aries` 0.5.0;
- PyPI declared package license: MIT;
- source distribution: `up_aries-0.5.0.tar.gz`;
- source-distribution SHA-256: `d552f29082e36d87962c5f4e101f818be4d47a7406a9d573eeb9004a8c850dd1`;
- PyPI reports this source distribution was uploaded without Trusted Publishing, so Nika does not invent a stronger publication-attestation claim;
- current maintained upstream is `plaans/aries`; that repository exposes the Unified Planning plugin and is dual-licensed Apache-2.0 OR MIT at the upstream source-tree level.

The historical `aiplan4eu/up-aries` repository is archived and explicitly points development to the main Aries repository; it is not treated as the current maintained source.

## Upstream runtime dependency caveat

The exact `up-aries` 0.5.0 packaging metadata, and the current maintained upstream plugin metadata checked on 2026-08-24, declare runtime requirements for `unified_planning`, `grpcio`, `grpcio-tools`, and `pytest` without exact version bounds. Therefore the two direct adopted pins do **not** by themselves constitute an immutable transitive lock.

Fresh Ubuntu and Windows CI resolution for the exact planning branch observed `grpcio==1.83.0`, `grpcio-tools==1.83.0`, `ConfigSpace==1.2.2`, `networkx==3.6.1`, `pyparsing==3.3.2`, `protobuf==7.36.0`, `numpy==2.5.2`, `scipy==1.18.1`, `more-itertools==11.1.0`, and related shared dependencies. Those versions are resolver evidence for that run, not a claim that upstream metadata pins them.

Aries also declares `pytest` as a runtime dependency. GitHub Advisory `GHSA-6w46-j5rx-g56g` / `CVE-2025-71176` marks pytest versions below 9.0.3 affected by insecure temporary-directory handling and identifies 9.0.3 as the patched boundary. Nika therefore raises its development/test floor to `pytest>=9.0.3,<10`; exact CI must prove compatibility before this change receives integration credit. This is a security floor, not a legal or compatibility conclusion about future pytest releases.

A future immutable planning-closure mechanism must be a narrow project-wide dependency/release decision, not an ad-hoc pile of transitive pins injected into the optional extra. Until that mechanism exists, release evidence must record the exact resolved closure and refresh it after any resolver, Python, platform, source, or dependency change.

## Rejected default engine

Pyperplan is not in the adopted `planning` extra. The Unified Planning wrapper surface can advertise a Pyperplan extra, but the Pyperplan 2.1 engine itself is GPLv3+. Nika therefore does not silently pull that engine into the default adopted path or mislabel its transitive license.

## Distribution/package boundary

The exact planning dependencies remain under `[project.optional-dependencies].planning`. They are absent from `[project].dependencies`, so the ordinary Windows base package/install does not acquire the planning engine merely because Deterministic Brain supports it.

Any future version/source/license change is a new adoption decision and must refresh dependency, license, provenance and Windows evidence before release credit.
