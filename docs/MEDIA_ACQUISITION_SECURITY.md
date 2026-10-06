# Media acquisition security boundary

Status: DEV05 binding implementation note for the `nika_core.media` remote-acquisition adapter.

## Reuse decision

- **ADAPT** maintained `yt-dlp` behind Nika-owned typed media contracts. Nika does not implement extractors or downloader/resume mechanics.
- `yt-dlp` is invoked as the pinned Python package with `sys.executable -m yt_dlp` through `SafeProcessRunner`.
- **REUSE** Nika's existing `promote_partial_file`, hashing and safe-process primitives for byte validation, atomic publication, timeout/cancel and process-tree cleanup.
- Nika owns URL/auth/privacy/resource policy, normalized errors, bounded metadata, durable publication semantics and persisted provenance.
- `pysubs2` remains the maintained subtitle parser/normalizer; this boundary does not replace it.

## Remote URL boundary

Remote discovery and byte acquisition accept HTTP(S) only. Before any subprocess starts, Nika rejects:

- URL-embedded username/password credentials;
- credential-like query parameters such as token, access-token, password, cookie, signature or API-key values;
- NUL/CR/LF and oversized URLs;
- loopback, private, link-local, reserved or otherwise non-global literal IP targets by default;
- `localhost` names by default.

Private-network media is an explicit policy opt-in and remains separate from authentication. URL credentials are never accepted as an authentication mechanism; future authenticated acquisition must resolve an opaque credential reference through a separate product action. Browser profiles and `--cookies-from-browser` are not loaded automatically.

The literal-address/localhost policy is a defense-in-depth boundary, not a claim of a complete network sandbox. DNS rebinding and arbitrary hostname resolution cannot be made safe by string validation alone; higher-assurance untrusted-network execution requires a separately proven network-confinement layer.

## Fixed yt-dlp capability surface

The discovery adapter uses a fixed argv containing `--ignore-config`, `--dump-single-json`, `--skip-download`, `--no-warnings` and normally `--no-playlist`. It does not expose arbitrary yt-dlp CLI arguments and does not use `--exec`, `--netrc-cmd`, `--write-link`, `--write-url-link`, `--write-desktop-link`, external downloaders or browser-cookie loading.

The byte-acquisition adapter also uses a fixed argv. It passes the stable source page URL rather than a previously discovered extractor/CDN URL, adds `--continue`, a bounded `--max-filesize`, a deterministic `-o` path, and an optional strictly validated format expression. It explicitly disables info-json, comment, thumbnail and subtitle side outputs for the media-byte action. A format value cannot introduce whitespace, shell syntax or a new command-line option.

The pinned package is `yt-dlp==2026.7.4`. This supersedes 2026.6.9 after upstream published 2026.7.4 with a security fix for the `--write-link` family. Nika does not use that affected option, but the media optional dependency should not deliberately remain on a superseded security release.

## Durable byte publication and restart

Remote media bytes are never published directly as a completed Nika asset. For a stable `version_id` plus selected format, Nika derives a deterministic filename inside a caller-owned output root:

1. yt-dlp downloads to `<name>.media.partial` and may retain its own `<name>.media.partial.part` resume file;
2. timeout/cancellation is handled by the existing `SafeProcessRunner` process-tree boundary;
3. a bounded `.part` file is deliberately retained after timeout/cancel so an explicit retry can use yt-dlp `--continue` rather than restart from zero;
4. an already oversized resume file fails before another subprocess starts;
5. after yt-dlp exits successfully, Nika requires the exact expected `.partial` file;
6. `promote_partial_file` enforces the configured maximum byte size, computes SHA-256 and optionally requires an expected checksum;
7. only then does `os.replace` atomically publish the final immutable `MediaAsset`;
8. an existing final asset, ambiguous completed partial, checksum mismatch or missing expected output fails closed rather than overwriting or manufacturing success.

The output root itself must already exist and all promoted paths are constrained beneath it. The Nika asset records only a relative local path, checksum, size, version identity and immutable-original flag. Upstream signed/CDN download URLs are not persisted as the restart mechanism; a future fresh acquisition reuses the stable source-page identity and rediscovery/extraction behavior.

The first durable byte action intentionally handles the original media asset only. Subtitle discovery and subtitle-first selection remain separate existing contracts; downloading subtitle bytes through the same durable publication rules is the next extension rather than allowing discovered signed subtitle URLs to become durable credentials.

## Metadata and provenance

`SafeProcessRunner` bounds yt-dlp stdout. Nika additionally bounds playlist entry count, duration, format catalog size and subtitle-track catalog size. Exceeding a configured metadata catalog limit fails closed with `metadata_limit`; catalogs are not silently truncated.

Persisted source/subtitle URLs are sanitized separately from executable input: URL userinfo is removed, credential-like query values are replaced with redaction markers, fragments are dropped, and stable IDs are derived only from sanitized provenance. Upstream extractor objects never cross the Nika media contract boundary.

## Evidence boundary

Normal CI does not download media, models, FFmpeg/Tesseract binaries or browser profiles. Durable-acquisition tests use fake process runners and local fixture bytes; they prove argv policy, retry-state handling, byte/checksum enforcement and atomic publication semantics, not availability of any external media service. Focused DEV05 Ubuntu and Windows jobs verify the exact PR head, installed media package identities, compile/import, Ruff and deterministic media tests. A green job is not human/NVDA, real-engine, external-service or physical-target-machine evidence.
