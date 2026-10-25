# Media URL credential policy

Status: focused Media security contract for `YtDlpAdapter`.

## Purpose

Remote media discovery accepts ordinary public HTTP(S) URLs, but URL-embedded credentials are not
persistence-safe provenance and must not be handed to the yt-dlp subprocess implicitly. Authenticated
media remains a separate product action using an opaque credential reference; browser cookies and
profiles are never loaded automatically by this adapter.

The same policy applies at two trust boundaries:

1. caller-supplied source URLs before the yt-dlp process is invoked;
2. upstream `webpage_url` / `original_url` metadata before it becomes `MediaSource.locator` or
   sanitized metadata.

A credential-like URL must therefore either fail closed before transport or be minimized before any
persistence-safe projection. Raw credential material is never accepted merely because upstream
metadata returned it.

## Query-key identity

Query parameter names are compared after deterministic Nika-owned normalization:

- Unicode-preserving `casefold()` for case identity;
- ASCII hyphen `-` is normalized to underscore `_` so common API spelling variants share one policy.

The bounded sensitive vocabulary includes existing token/key/password/signature identities plus
common aliases such as `client_secret`, `subscription_key`, `x_api_key`, `auth_token` and selected
cloud credential identifiers. Bounded suffixes `_token`, `_secret`, `_password` and `_signature`
are also sensitive.

This is deliberately not a blanket query-string ban. Ordinary non-credential metadata remains
supported. In particular, `subscription=public-catalog` is a valid benign query and must not be
misclassified as `subscription-key`.

## Admission behavior

`YtDlpAdapter.discover()` validates the source URL before constructing a subprocess effect. A URL
containing URL userinfo or a credential-like query key raises `MediaError` with
`MediaErrorCode.AUTH_REQUIRED`. The error text is generic and must not echo the credential value.
No yt-dlp runner call is allowed on this path.

Supplying `auth_ref` also fails closed in this adapter because credential resolution is an explicit
higher-level product action. This module does not enumerate credentials, read browser state, or turn
an opaque reference into a secret.

## Upstream metadata minimization

Upstream canonical URLs are untrusted provider metadata. `_sanitize_url_for_persistence()` removes
userinfo and rewrites sensitive query values to `[REDACTED]` using the same normalized key decision
as admission. The resulting safe URL, not the raw upstream URL, participates in `MediaSource`
identity and sanitized metadata.

Using one decision function for admission and persistence prevents spelling drift where a credential
alias is blocked in one path but preserved in the other.

## Reuse decision

- **REUSE:** existing `YtDlpAdapter` / `YtDlpPolicy`, `MediaErrorCode.AUTH_REQUIRED`, Python
  `urllib.parse` query parsing, and existing Media privacy projection.
- **ADAPT:** one normalized Media query-key classifier shared by source admission and persistence
  sanitization.
- **CUSTOM (thin):** only the bounded credential vocabulary and focused deterministic regressions.

No second URL parser, credential store, sanitizer framework, browser profile loader, downloader or
network stack is introduced.

## Acceptance evidence

Independent QA-only PR #480 is the canonical adversarial oracle for this family. It uses synthetic
canaries only and requires fail-closed admission for `api-key`, `client_secret`, `client-secret`,
`subscription-key`, `subscription_key` and `x-api-key`; it also checks upstream persistence
minimization and the benign `subscription=public-catalog` control.

Current-main production successor #488 owns the narrow repair. Production and QA receive no
acceptance credit from stale SHAs: exact Core Ubuntu/Windows, DEV05 Media Foundation and complete M12
must pass on the final production head, then the unchanged #480 oracle must be replayed against that
exact production parent before independent AUD03/QA53 classification.

This backend security repair is not a packaged Product Journey claim. `HUMAN_TESTED=false` and
`NVDA_VERIFIED=false` remain human-only truths.
