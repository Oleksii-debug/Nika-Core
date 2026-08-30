# Audit secret-key normalization hardening

## Scope

This note extends `docs/AUDIT_INTEGRITY.md` for the generic `AuditLog` secret-minimization
boundary. It does not change R0-R4 authorization, approval provenance, or the active M10
ownership in PRs #61/#62.

## Key-name defect

The original redaction matcher case-folded payload keys and replaced `-` with `_`.
That covered canonical names such as `access_token` and `api-key`, but it did not cover
common equivalent key styles such as:

- `accessToken`;
- `APIKey`;
- `private.key`;
- prefixed forms such as `clientAPIKey` and `nestedRefreshToken`;
- other prefixed credential forms such as `requestAuthorization`, `browserCookie`,
  `userSession`, `dbPasswd`, and `httpCookieHeader`.

Those values could therefore be serialized into `audit_events.payload_json` before the
integrity envelope was calculated. Integrity chaining does not make plaintext secret
persistence acceptable.

## Key-name repair

`AuditLog` normalizes a candidate key before sensitive-key comparison by:

1. splitting acronym-to-word boundaries such as `APIKey -> API_Key`;
2. splitting lower/digit-to-uppercase camel boundaries such as
   `accessToken -> access_Token`;
3. replacing non-ASCII-alphanumeric separator runs with `_`;
4. trimming separators and applying `casefold()`;
5. matching the exact sensitive vocabulary and prefixed forms whose normalized key
   ends with the complete sensitive vocabulary.

The original payload key spelling is preserved in the stored redacted object; only the
matching representation is normalized. This avoids silently rewriting application audit
schemas while preventing the secret value from reaching SQLite.

Count-like metadata remains ordinary data because normalization is token-boundary based.
For example, `tokenCount`, `sessionCount`, and `cookieCount` normalize to `_count` forms,
not to a sensitive key or sensitive-key suffix.

## Embedded-value defect and repair

A separate independent leakage oracle demonstrated that a benign audit key such as
`message`, `detail`, `error`, `stderr`, or `url` could still carry credential material
inside its string value. Key classification alone therefore did not prevent persistence
of common diagnostic forms such as an Authorization/Bearer header, `api_key=...`, URL
userinfo, a sensitive URL query parameter, a Git credential URL, or a Cookie header.

String leaves are now sanitized before canonical JSON serialization and before the event
digest is calculated. The sanitizer is deliberately contextual rather than a generic
word filter:

- URL userinfo in URI forms is replaced with `[REDACTED]`;
- Authorization/Proxy-Authorization values and standalone Bearer credentials are
  redacted;
- credential assignments such as access/refresh token, API key, client secret,
  private key, password/passwd/pwd, session ID/token, and credential are redacted;
- Cookie and Set-Cookie header values are redacted.

This preserves useful surrounding diagnostic text while removing the credential-bearing
segment. Ordinary strings such as `tokenCount=7 sessionCount=4 cookieCount=2` remain
unchanged. Existing sensitive-key behavior still replaces the entire sensitive value and
retains the original key spelling.

Redaction remains defense in depth, not permission for callers to log arbitrary raw
credentials. Synthetic canaries are used in tests; no real secrets belong in fixtures.

## Evidence

`tests/test_audit_secret_key_redaction_variants.py` and
`tests/test_audit_integrity_adversarial.py` exercise key normalization plus value-level
secret minimization. They verify original public key spelling, `[REDACTED]` substitution,
non-secret count preservation, and raw `audit_events.payload_json`, asserting that
supplied synthetic secret values do not exist at rest.

Independent QA_ONLY secret-leakage oracles remain external acceptance evidence and must
not be merged into production merely to obtain green.

Repository acceptance still requires exact-head Core CI on Ubuntu and Windows plus the
applicable M12 gate. AUD02 remains independent acceptance authority for this security
change.

## REUSE -> ADAPT -> CUSTOM

- REUSE Python standard-library `re` and the existing recursive redaction boundary.
- ADAPT the existing sensitive-key vocabulary to common key naming representations and
  contextual credential-bearing diagnostic forms.
- CUSTOM remains thin Nika audit minimization policy only.

No dependency, migration, permission, credential authority, or approval level is added.
