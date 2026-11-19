# Plan 1 Section 3: verified configuration and capability authority

Independent 3А / 3Б / 3В and legacy aggregate Section 3 are DONE.
This is repository-controlled acceptance of the existing configuration and
capability boundary. Physical NVDA, model hardware and live-cloud proof keep
their separate final acceptance gates.

| Required boundary | Existing authority and verification |
|---|---|
| Typed settings and environment overrides | `src/nika_core/config.py`: Pydantic Settings, NIKA_* inputs, validated defaults/log/provider, explicit absolute database path |
| Consistent durable location | `tests/test_config_db_alias_conflict.py`: case-insensitive/single/equal aliases; divergent paths rejected before default/adoption; Unicode and spaces retained |
| Supported version | Exact schema1; explicit bool/string/float and unsupported versions denied; canonical environment version accepted |
| Accessible startup admission | Current `scripts/nika_windows.py` catches settings/validation errors before bridge/PF11/shell effects; Ukrainian message omits private values |
| Existing environment/default behavior | Foundation and stable packaged data-location tests retained; configuration correction permits normal startup admission again |
| Capability registry | `src/nika_core/runtime/registry.py`: immutable descriptor/effect identity snapshots, exact enum carriers and callable port admission |
| Shared runtime identity | Same current constructor validator: exact strings, NFC, no control/format/surrogate text, at most512UTF8 bytes; registry lookup keeps KeyError contract |
| Structural drift | `tests/test_runtime_registry_authority.py`: identity/capability expansion and callable/noncallable effect replacement denied by get/select/describe |
| Independent diagnostics defense | Valid config deliberately corrupted in the health fixture; configuration FAIL and secret-canary exclusion remain asserted |

The environment contract is the existing typed NIKA_* configuration and
explicit settings; the plan title does not justify a second named-profile or
capability authority. The intentional compatibility correction denies conflicting
database aliases instead of silently preferring one. Existing equal/single
aliases, explicit path precedence and valid default model-free/mock operation
remain exercised by the real regression suite.

Canonical #847 source18141d1f passed Core43966273629 and M1243966273641,
then integrated7d5d9c46. Canonical #1103 source401aae34 passed
Core43967718231, M1143967718393 and fullM1243967718206; Linux3049passed/
5skipped, M12Windows3047passed/7skipped, then integrated7c340e33.
Independent remote changed-source reviews found no blocking findings.
The exact scoped main tree was constructed before each merge and matched
GitHub readback, preserving already accepted source and closure documentation.
Full run/job/artifact/ZIP identities are in MULTI_PLAN_CLOSURE_STATE.md.

No secrets or credential material were added. Rollback preserves all prior
source versions in Git history. Human/NVDA and production readiness remain false.
