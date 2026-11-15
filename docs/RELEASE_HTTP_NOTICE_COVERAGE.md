# Exact-package HTTP notice coverage repair

## Observed defect

M11 run `43754316737`, source `360bca7f75c824b0a380375998fe893f6c17f784`,
produced final Windows ZIP SHA-256
`bcf482d8a1df35baa00c46078250b9e7d0e006f6498e6382b3911b2b9233be1c`.
The artifact download was verified against its Actions digest, and the EXE
was inspected without execution using PyInstaller 6.22.3's archive reader.
EXE SHA-256: `5925db710caaaf00d7299f7ac191cfb68d837be70eb60d5f40325e2c0f4a007c`.

| Actual PYZ namespace | Module count | Old corresponding notice |
|---|---:|---|
| httpx | 23 | missing |
| httpcore | 31 | missing |
| anyio | 43 | missing |
| h11 | 11 | missing |
| idna | 6 | missing |
| certifi | 2 | missing |

The EXE has 1377 PYZ modules. Constants confirm HTTPX 0.28.1, HTTPCore 1.0.9
and Certifi 2026.10.23. The ZIP also ships Certifi's 235863-byte `cacert.pem`,
SHA-256 `2ef52ad56b4803e264045c915e55c236cbfa045b84b81efefc218a19eb44bd2b`.
The old notices have 21 sections including Python. Producer and verifier both
iterate the same fixed list, so they agreed despite this omission.

No pypdf/docx/openpyxl/defusedxml/lxml/et_xmlfile modules were found in that PYZ.
Their installation during a build is not evidence of redistribution.

## Compatibility and reuse decision

This is the existing PR #1089 notice-reader/publication lineage, converged with
Plan 1 candidate #1871. Preserve its bounded, strict-UTF-8, descriptor/path,
Windows reparse-point, atomic-publication and corruption protections. No new
notice, compliance, runtime or SBOM service is introduced. Existing #695 owns
the broader SBOM/provenance contract; this repair does not copy or supersede it.
The shared change repairs a demonstrated Plan 6 packaging defect needed by
Plan 1's dependency-notice acceptance; it does not silently close either plan.

Add the six observed distributions to the canonical notice producer/verifier.
Collect their actual installed license/notice texts. Certifi additionally
receives an exact-version upstream source-package reference alongside its MPL
license text; the reference is recomputed by the verifier and URL-escaped.
The coverage regression uses the independently observed six-component set,
then removes each section and verifies denial plus successful regeneration.

The Plan 1 collector's fake distribution fixture is updated to implement the
standard `locate_file("")` root behavior used by the stronger existing reader.
It still asserts that executable/source/cache candidates are never read.

## Required qualification

Full source checks, Ubuntu/Windows Core, M11 and complete M12 must pass on this
combined head. Inspect the regenerated exact final ZIP for all six attribution
sections, genuine license texts and the Certifi source link. This fixes the
observed coverage defect; it is not blanket clearance of every optional engine,
native component or model, and does not establish physical NVDA acceptance.

## Proven Windows convergence failure and contained repair

First combined head `c84e411393baefddf0d3c661869e2b0bf06f9a21` passed Core/M12
Ubuntu but failed Windows Core, M11 and M12 while observing valid installed
license files (jobs `132938283808`, `132938283477`, `132938283646`). The inherited
reader compared pathname and descriptor `ctime_ns` as though their meanings
matched. CPython 3.12's pathname stat preserves creation-time compatibility;
descriptor fstat retains metadata ChangeTime. The official source locations are
[pathname stat](https://github.com/python/cpython/blob/v3.12.16/Modules/posixmodule.c#L2139-L2149)
and [descriptor stat](https://github.com/python/cpython/blob/v3.12.16/Python/fileutils.c#L1116-L1123).

The same reader now compares the common Windows birthtime for path/descriptor
identity, while retaining descriptor ctime stability across both reads. Device,
file ID, size, mtime, content equality, no-follow/reparse and sharing restrictions
remain enforced; POSIX comparisons remain unchanged. Sanitized failure reasons
identify the check and canonical distribution-relative file, without exposing
absolute environment paths or file contents. Synthetic ctime compatibility and
descriptor-change denials complement the required actual Windows rerun.
