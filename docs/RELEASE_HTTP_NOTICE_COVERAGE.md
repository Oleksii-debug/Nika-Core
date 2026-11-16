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

That correction admitted installed Windows licenses; M11 job `132941485163`
then exposed a separate inherited LF/CRLF mismatch: section parsing canonicalized
lines while upstream Python/pydantic-core/tomli license text retained CRLF.
After both bounded raw-byte reads and all identity checks, strict UTF-8 text now
normalizes CRLF to LF. The canonical writer and comparison agree, without
relaxing byte integrity or UTF-8 validation. LF/CRLF generation-verification and
subsequent altered-license rejection have explicit regressions.

## Complete bounded inventory and ambient-runner repair

The subsequent exact source6205866 ZIP, SHA-256
159b04424c889ed467b70619bca354b1ca63880d5c3cfd9bce4d301351be4330,
confirmed all six HTTP notices, genuine license texts and the Certifi source
reference. Its full 1377-module PYZ had 32 nonstdlib/non-Nika roots mapping
unambiguously to 30 distributions. Four further canonical licenses were absent:
markdown-it-py4.3.0, mdurl0.1.2, pytest8.4.2 and top-level wheel0.48.0. All four
are pinned in the selected packaging/build exports; the latter is not satisfied
by a different setuptools-vendored wheel0.46.3 notice. They are now inventoried
together, with independent omission/regeneration tests.

Click8.5.0 was also frozen from outside the selected packaging/build locks.
Its separate bundled BSD license was present, so this was an acquisition/
reproducibility gap rather than an absent-license finding. REUSE Python stdlib
venv for fresh Core/M11/M12 profile installs; verify the active prefix and
disabled user site before installing exact hashed dependencies. Clear inherited
PYTHONPATH, and refuse existing environment state rather than delete or reuse it.
This prevents undeclared runner packages entering the frozen runtime; it does
not add Click as a new product dependency.

REUSE PyInstaller's archive reader plus the existing canonical notice verifier
to inspect actual frozen roots against the independent audited owner mapping.
Unknown roots and missing owner notices fail closed. M11/M12 inspect the built
bundle, and M12 repeats this on the extracted exact final ZIP. This is a narrow
executable coverage proof, not a second SBOM/compliance format or a claim about
absent optional engines, models or document parsers.

The local execution environment became unavailable before this follow-on
patch could be locally qualified. This patch requires its own hosted exact-head
Linux/Windows, package and final-artifact proof; prior 2921-test evidence applies
only to source6205866.

Independent review of the first isolated-build patch caught an uncontrolled
second notice-file read in the new inspector. The inspector now stops on
canonical verifier findings, then checks actual PYZ owners against the inventory
whose exact license bodies were already proved by that bounded verifier. It
does not reopen the notice file or duplicate the trusted validation logic.
Rejected-evidence regressions prohibit an unsafe second read.

## 2026-11-16 exact-package review and remaining causal repairs

Source `2f92a1203a754a3e4a420b65eb0485455f499f6e` passed clean hashed local
verification (2931 passed, 5 skipped), Core Ubuntu/Windows and M11 run
`43782890671`. Artifact `14188774853` SHA-256
`7c710c58d8acf4be13dc8f1f7204872c15f35a9b228b5e9dcb367067e14c7d35`
contained final ZIP SHA-256
`3f64d3a83dcd96a4b96078f958233a3cd3654491dbf89921ba48cc05a88d0119`.
All 110 manifest entries matched their actual sizes/hashes and source identity.
Independent PyInstaller inspection found 1360 PYZ modules, 31 third-party roots,
no ambient Click and 31 notice sections. Counts alone were not acceptance:
independent review found the proxy_tools section contained only metadata.

### Missing proxy_tools license text

The official [0.1.0 release](https://pypi.org/project/proxy_tools/0.1.0/) contains
only an sdist, SHA-256
`ccb3751f529c047e2d8a58440d86b205303cf0fe8146f784d1cbcd94f0a28010`.
It omits LICENSE.txt and declares MIT in package metadata, while its sole module
explicitly says BSD and attributes Armin Ronacher and Jonathan Tushman. That
module matches upstream commit `db43f1e35d4f90a65c5a4d56d9e9af88212ec6e6`,
SHA-256 `d1539d95e1a713c068ca81d42e047b2c76568964cf277596d4e19efb22f476be`.
The exact [upstream license](https://github.com/jtushman/proxy_tools/blob/db43f1e35d4f90a65c5a4d56d9e9af88212ec6e6/LICENSE.txt)
has SHA-256 `a428fb8a2e762af3eb0a6edbbb88e9b42ccfee80fd9b423958bcacf9b9abbfe4`.

The canonical notice generator now retains that upstream text unaltered,
including its historical trailing wording, copyright and redistribution
conditions. It preserves the conflicting metadata as a declaration rather than
inventing an MIT grant. This offline exception requires version 0.1.0 and the
reviewed module identity through the existing bounded/no-follow reader (strict
UTF-8 and CRLF-to-LF canonicalization); any changed version/source fails closed.
Metadata-only notices for other packages now fail instead of passing the
generator and verifier together. Tests cover denial, source/version mismatch,
missing source, exact license preservation and recovery after restored source.
The upstream's 2014 release is a retained legacy transitive pywebview dependency,
not a claim of active upstream maintenance or a newly adopted engine.

### Actual Windows UIA fixture failure

M12 Windows job `133023967001` passed 2929 source tests (7 skipped) but its M9
fixture could not find a window for the subprocess PID. The fixture was launched
through the fresh venv redirector. CPython's maintained
[spawn implementation](https://github.com/python/cpython/blob/v3.12.10/Lib/multiprocessing/popen_spawn_win32.py#L61-L84)
explicitly bypasses that redirector while preserving the venv and returning the
real child PID. The fixture now reuses `multiprocessing` with `spawn`, keeping
process-scoped UIA and bounded terminate/join/kill cleanup. The root-cause link
is an inference from exact failure plus upstream source until a fresh real
Windows run passes. No global-window lookup, coordinate fallback or environment
isolation bypass was added. Success/failure and forced-cleanup cases are tested.

These repairs require new exact-head Core and complete M12/M11 qualification.
Section 1 remains nonterminal until applicable independent provenance review,
fresh gates and ordered main readback. Actual Python-owner notice coverage does
not assert every native DLL/model license or physical NVDA acceptance.

## Independent exact-version provenance review — 2026-11-16

The 30 observed shipped distribution versions were independently queried at
their official PyPI version endpoints and compared with the hashed build/
packaging exports. Every version exists and every row has at least one matching
upstream distribution SHA-256 in the lock exports. Independent review also
found no yanked release files. License/notice texts for the actual Python owners
were inspected separately from metadata; the proxy_tools omission/conflict above
is the concrete repaired exception. This is a bounded dependency adoption review,
not a claim that merely collecting metadata grants license clearance.

| Distribution | Exact version | Release upload date | Reviewed license/notice family |
|---|---|---|---|
| [annotated-types](https://pypi.org/pypi/annotated-types/0.8.0/json) | 0.8.0 | 2026-07-23 | MIT |
| [anyio](https://pypi.org/pypi/anyio/4.16.0/json) | 4.16.0 | 2026-11-01 | MIT |
| [bottle](https://pypi.org/pypi/bottle/0.13.4/json) | 0.13.4 | 2025-06-15 | MIT |
| [certifi](https://pypi.org/pypi/certifi/2026.10.23/json) | 2026.10.23 | 2026-10-23 | MPL-2.0 |
| [cffi](https://pypi.org/pypi/cffi/2.1.1/json) | 2.1.1 | 2026-08-03 | MIT-0 |
| [clr-loader](https://pypi.org/pypi/clr-loader/0.3.1/json) | 0.3.1 | 2026-04-18 | MIT (upstream text) |
| [h11](https://pypi.org/pypi/h11/0.16.0/json) | 0.16.0 | 2025-04-24 | MIT |
| [httpcore](https://pypi.org/pypi/httpcore/1.0.9/json) | 1.0.9 | 2025-04-24 | BSD-3-Clause |
| [httpx](https://pypi.org/pypi/httpx/0.28.1/json) | 0.28.1 | 2024-12-06 | BSD-3-Clause |
| [idna](https://pypi.org/pypi/idna/3.20/json) | 3.20 | 2026-09-17 | BSD-3-Clause |
| [markdown-it-py](https://pypi.org/pypi/markdown-it-py/4.3.0/json) | 4.3.0 | 2026-11-10 | MIT (upstream text) |
| [mdurl](https://pypi.org/pypi/mdurl/0.1.2/json) | 0.1.2 | 2022-08-14 | MIT (upstream text) |
| [packaging](https://pypi.org/pypi/packaging/26.3/json) | 26.3 | 2026-08-04 | Apache-2.0 OR BSD-2-Clause |
| [platformdirs](https://pypi.org/pypi/platformdirs/4.14.5/json) | 4.14.5 | 2026-11-04 | MIT |
| [proxy-tools](https://pypi.org/pypi/proxy-tools/0.1.0/json) | 0.1.0 | 2014-05-05 | Upstream BSD; metadata MIT (see correction above) |
| [pycparser](https://pypi.org/pypi/pycparser/3.12/json) | 3.12 | 2026-10-22 | BSD-3-Clause |
| [pydantic](https://pypi.org/pypi/pydantic/2.14.3/json) | 2.14.3 | 2026-11-05 | MIT |
| [pydantic-core](https://pypi.org/pypi/pydantic-core/2.50.4/json) | 2.50.4 | 2026-11-05 | MIT |
| [pydantic-settings](https://pypi.org/pypi/pydantic-settings/2.16.3/json) | 2.16.3 | 2026-11-13 | MIT |
| [pygments](https://pypi.org/pypi/pygments/2.21.0/json) | 2.21.0 | 2026-08-17 | BSD-2-Clause |
| [pytest](https://pypi.org/pypi/pytest/8.4.2/json) | 8.4.2 | 2025-09-04 | MIT |
| [python-dotenv](https://pypi.org/pypi/python-dotenv/1.2.4/json) | 1.2.4 | 2026-10-01 | BSD-3-Clause |
| [pythonnet](https://pypi.org/pypi/pythonnet/3.2.1/json) | 3.2.1 | 2026-10-08 | MIT |
| [pywebview](https://pypi.org/pypi/pywebview/6.2.1/json) | 6.2.1 | 2026-04-15 | BSD-3-Clause |
| [rich](https://pypi.org/pypi/rich/15.0.0/json) | 15.0.0 | 2026-04-12 | MIT |
| [setuptools](https://pypi.org/pypi/setuptools/84.2.1/json) | 84.2.1 | 2026-10-20 | MIT |
| [tomli](https://pypi.org/pypi/tomli/2.5.0/json) | 2.5.0 | 2026-10-07 | MIT |
| [typing-extensions](https://pypi.org/pypi/typing-extensions/4.16.0/json) | 4.16.0 | 2026-07-02 | PSF-2.0 |
| [typing-inspection](https://pypi.org/pypi/typing-inspection/0.4.5/json) | 0.4.5 | 2026-11-04 | MIT |
| [wheel](https://pypi.org/pypi/wheel/0.48.0/json) | 0.48.0 | 2026-08-11 | MIT |

The retained mdurl release dates to 2022 and proxy_tools to 2014; release
existence is not proof of active maintenance. These existing transitive helpers
remain pinned and exercised through the reviewed pywebview/Markdown consumers.
No new model/runtime engine or permissive-license claim is inferred for them.
Certifi uses its actual upstream MPL notice plus the exact-version unmodified
source reference. Python retains its full runtime notice. This table does not
award optional engines/models or separate native DLLs unperformed clearance.

Current repaired local source: 2938 passed, 5 skipped; pip consistency, Ruff and
compile passed. Independent focused review: 74 tests and Ruff passed. Exact new
hosted Windows/M12/final-ZIP qualification remains required.
