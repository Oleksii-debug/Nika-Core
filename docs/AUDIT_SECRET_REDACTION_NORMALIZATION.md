# Audit secret-key normalization hardening

## Scope

This note extends `docs/AUDIT_INTEGRITY.md` for the generic `AuditLog` secret-minimization
boundary. It does not change R0-R4 authorization, approval provenance, or the active M10
ownership in PRs #61/#62.

## Defect

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

## Repair

`AuditLog` now normalizes a candidate key before sensitive-key comparison by:

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

## Evidence

`tests/test_audit_secret_key_redaction_variants.py` and
`tests/test_audit_integrity_adversarial.py` exercise snake/camel/acronym/punctuation and
prefixed sensitive-key variants. They verify the original public key spelling, the
`[REDACTED]` value, non-secret count metadata, and raw `audit_events.payload_json`,
asserting that none of the supplied secret values exist at rest.

Repository acceptance still requires exact-head Core CI on Ubuntu and Windows plus the
applicable M12 gate. AUD02 remains independent acceptance authority for this security
change.

## REUSE -> ADAPT -> CUSTOM

- REUSE Python standard-library `re` and the existing recursive redaction boundary.
- ADAPT the existing sensitive-key vocabulary to common key naming representations.
- CUSTOM remains thin Nika audit minimization policy only.

No dependency, migration, permission, credential authority, or approval level is added.
