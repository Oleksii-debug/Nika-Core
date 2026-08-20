# Media acquisition security boundary

Status: DEV05 binding implementation note for the `nika_core.media` remote-acquisition adapter.

## Reuse decision

- **ADAPT** maintained `yt-dlp` behind Nika-owned typed media contracts. Nika does not implement extractors.
- `yt-dlp` is invoked as the pinned Python package with `sys.executable -m yt_dlp` through `SafeProcessRunner`.
- Nika owns URL/auth/privacy/resource policy, normalized errors, bounded metadata and persisted provenance.
- `pysubs2` remains the maintained subtitle parser/normalizer; this boundary does not replace it.

## Remote URL boundary

Remote discovery accepts HTTP(S) only. Before any subprocess starts, Nika rejects:

- URL-embedded username/password credentials;
- credential-like query parameters such as token, access-token, password, cookie, signature or API-key values;
- NUL/CR/LF and oversized URLs;
- loopback, private, link-local, reserved or otherwise non-global literal IP targets by default;
- `localhost` names by default.

Private-network media is an explicit policy opt-in and remains separate from authentication. URL credentials are never accepted as an authentication mechanism; future authenticated acquisition must resolve an opaque credential reference through a separate product action. Browser profiles and `--cookies-from-browser` are not loaded automatically.

The literal-address/localhost policy is a defense-in-depth boundary, not a claim of a complete network sandbox. DNS rebinding and arbitrary hostname resolution cannot be made safe by string validation alone; higher-assurance untrusted-network execution requires a separately proven network-confinement layer.

## Fixed yt-dlp capability surface

The discovery adapter uses a fixed argv containing `--ignore-config`, `--dump-single-json`, `--skip-download`, `--no-warnings` and normally `--no-playlist`. It does not expose arbitrary yt-dlp CLI arguments and does not use `--exec`, `--netrc-cmd`, `--write-link`, `--write-url-link`, `--write-desktop-link`, external downloaders or browser-cookie loading.

The pinned package is `yt-dlp==2026.7.4`. This supersedes 2026.6.9 after upstream published 2026.7.4 with a security fix for the `--write-link` family. Nika does not use that affected option, but the media optional dependency should not deliberately remain on a superseded security release.

## Metadata and provenance

`SafeProcessRunner` bounds yt-dlp stdout. Nika additionally bounds playlist entry count, duration, format catalog size and subtitle-track catalog size. Exceeding a configured metadata catalog limit fails closed with `metadata_limit`; catalogs are not silently truncated.

Persisted source/subtitle URLs are sanitized separately from executable input: URL userinfo is removed, credential-like query values are replaced with redaction markers, fragments are dropped, and stable IDs are derived only from sanitized provenance. Upstream extractor objects never cross the Nika media contract boundary.

## Evidence boundary

Normal CI does not download media, models, FFmpeg/Tesseract binaries or browser profiles. Focused DEV05 Ubuntu and Windows jobs verify the exact PR head, installed media package identities, compile/import, Ruff and deterministic media tests. A green job is not human/NVDA or physical-target-machine evidence.
