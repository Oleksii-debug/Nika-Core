# Local speech output / TTS

## Scope

This module is the bounded Windows-first speech-output boundary for Nika Core: the product's local
"mouth" capability. It is deliberately separate from Media ingestion/ASR and does not create a new
model, network, permission, UI, or agent-runtime authority.

The production adapter uses the Windows-installed `System.Speech.Synthesis.SpeechSynthesizer`
through the trusted Windows PowerShell executable. It does not download voices or models and does
not access a cloud endpoint.

## REUSE -> ADAPT -> CUSTOM(thin)

- **REUSE:** Windows `System.Speech` installed voices, Windows PowerShell, Python stdlib JSON,
  subprocess, threading and timeout primitives.
- **ADAPT:** expose installed local voices and plain-text synthesis behind the Nika-owned
  `SpeechOutputPort` contract.
- **CUSTOM(thin):** input bounds, exact voice evidence, process-wide overlap guard,
  timeout/cancellation, trusted executable resolution, response validation and privacy-safe errors.

No dependency or packaging change is required.

## Privacy and safety invariants

1. Speech text is sent to the local PowerShell host over UTF-8 `stdin`; it is never placed in argv.
2. Public failures expose only stable error codes/messages and process exit code. PowerShell stderr
   and the spoken text are not copied into errors or receipts.
3. A receipt contains only engine/voice identity, character count, rate and volume; it does not
   persist the text.
4. The adapter has no network API, model downloader, credential input, file output or SSML surface.
5. A process-wide nonblocking lock rejects overlapping Nika speech rather than mixing utterances.
6. Cancellation before dispatch is effect-free. Cancellation/timeout after dispatch terminates the
   local process tree and is reported explicitly; callers must not claim the utterance completed.
7. Voice identity is exact and case-insensitive for requested-vs-reported comparison; duplicate
   installed voice identities fail closed.
8. The default production executable is resolved from the Windows directory via Win32 API rather
   than `PATH`/`cwd`; a reparse-point PowerShell executable is rejected.

## Accessibility boundary

Speech output is **not automatic** in this batch. There is no UI/runtime hook that reads every Nika
message aloud. That is intentional for NVDA-first operation: product integration must provide a
clear user-controlled enable/action boundary and avoid double-speaking with a screen reader.

Automated tests cannot set `HUMAN_TESTED` or `NVDA_VERIFIED`. A later packaged integration must be
checked on physical Windows with NVDA for focus behavior, discoverability, cancellation and
interaction with the user's selected screen-reader speech settings.

## Contract summary

`SpeechRequest` accepts Unicode plain text, optional installed voice identity, `rate=-10..10`, and
`volume=0..100`. Text is bounded to 20,000 characters and NUL is rejected.

`WindowsSystemSpeechAdapter.list_voices()` returns immutable local voice metadata.
`WindowsSystemSpeechAdapter.speak()` returns an immutable non-text receipt only after the backend
reports successful completion with the exact selected voice identity.

Timeouts are bounded to at most one hour. Engine busy, cancellation, timeout, unsupported platform,
invalid request and invalid engine response are distinct typed error states.

## Non-claims

- no cloud or external TTS provider;
- no speech-recognition/ASR changes;
- no new voice/model installation;
- no SSML or arbitrary PowerShell script execution API;
- no UI/keymap/permission/approval integration;
- no packaged physical-Windows TTS proof yet;
- `HUMAN_TESTED=false`;
- `NVDA_VERIFIED=false`.
