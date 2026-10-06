# M10 target-scope hardening

Updated: 2026-08-19.

Status: implementation candidate. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.

## Scope

This slice hardens the already integrated M10 downstream `SandboxPolicy` after the durable
Toolsmith→M10 bridge exposed two generic ambiguity classes:

1. platform-dependent parsing of Windows-style write paths when policy tests run on Linux;
2. executable allowlists that previously reduced every configured entry to its basename.

The change stays entirely inside M10 security policy, its focused tests and this document. It does
not edit M1-M4, Toolsmith, runtime, storage or GUI implementation.

## REUSE / ADAPT / CUSTOM

REUSE:

- Python `PureWindowsPath` and `PurePosixPath` for lexical path parsing independent of the host OS;
- existing concrete `Path.resolve()` containment check after lexical validation;
- existing M10 `SecurityPolicy`, resource budgets and approval flow.

ADAPT:

- Windows naming rules are applied as a deterministic precondition even when the test runner is
  Linux, because Nika's primary packaged target is Windows;
- executable entries now have two explicit meanings: a bare executable name is a basename grant,
  while an absolute Windows/POSIX path is an exact path-scoped grant.

CUSTOM thin:

- fail-closed rejection for ambiguous write targets and executable scopes at the M10 boundary.

No new dependency is added.

## Write-target invariants

Before a target reaches concrete filesystem resolution, M10 now parses both Windows and POSIX
semantics. A write target or writable root is rejected when it is empty/current-directory scope,
absolute/rooted/drive-qualified, contains parent traversal, or uses a Windows-reserved target.

Windows-first rejection includes:

- `.git` as any path component, case-insensitively;
- NTFS alternate-stream / colon syntax;
- reserved filename characters `< > : " | ? *`;
- ASCII control characters;
- trailing spaces or periods that Win32 may normalize ambiguously;
- reserved device names including `CON`, `PRN`, `AUX`, `NUL`, `COM1..COM9`, `LPT1..LPT9` and the
  documented superscript-digit variants, including when followed by an extension.

Backslashes are normalized as separators before the existing resolved-path containment check. This
makes `worktrees\\..\\secret` fail identically on Ubuntu CI and Windows rather than relying on the
host runner's native `Path` flavour.

This remains a policy boundary, not a claim that string validation eliminates filesystem TOCTOU,
reparse-point or hostile-kernel behavior. Concrete adapters still need the strongest practical OS
isolation and safe file-opening strategy appropriate to their threat model.

## Executable-scope invariants

Backward-compatible bare-name grants remain supported. For example, an allowlist entry `pytest`
may authorize an absolute executable whose basename is `pytest`.

A path-scoped allowlist is stricter:

- it must be absolute; relative `bin/python`, `./python` and drive-relative `C:python.exe` entries
  fail at policy construction;
- Windows absolute paths compare with Windows case-insensitive semantics;
- POSIX absolute paths compare case-sensitively;
- an exact path-scoped grant never falls back to basename matching;
- malformed or reserved requested executable strings fail as `PermissionError` rather than being
  treated as a different policy shape.

Path-scoped string equality is not binary identity. A deployment that needs to bind execution to a
specific binary must additionally use stronger provenance such as a verified executable digest and
OS-enforced isolation. The policy deliberately does not pretend otherwise.

## Primary-source basis

The implementation follows Python's documented distinction between host-dependent concrete `Path`
and host-independent `PureWindowsPath` / `PurePosixPath`, allowing Windows lexical semantics to be
checked on non-Windows CI.

Microsoft's Win32 naming rules define drive/UNC path semantics, reserved characters, reserved device
names, case-insensitive default behavior and the rule against trailing spaces/periods. M10 uses
those rules only as a conservative lexical boundary; it does not attempt to recreate the Windows
filesystem implementation.

## Acceptance gate

The exact candidate must prove on Ubuntu and Windows that:

1. Windows drive, rooted, UNC and backslash traversal forms fail cross-platform;
2. `.git`, ADS/colon, reserved devices, invalid characters and trailing-dot/space targets fail;
3. ordinary backslash workspace children normalize to the expected contained path;
4. invalid writable roots fail during policy construction;
5. bare-name executable grants keep documented compatibility;
6. exact POSIX path grants remain exact and case-sensitive;
7. exact Windows path grants remain exact and case-insensitive;
8. relative/ambiguous executable path grants fail closed;
9. the complete repository verification remains green with no weakened tests.

No `PACKAGED`, `HUMAN_TESTED` or `NVDA_VERIFIED` credit is claimed until the corresponding exact
candidate evidence exists.
