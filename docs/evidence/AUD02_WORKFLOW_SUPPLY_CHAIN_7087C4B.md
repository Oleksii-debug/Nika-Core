# AUD02 exact-parent workflow supply-chain replay

QA-only / do not merge as production repair.

Production target: PR #405 exact head `7087c4b036a90cdfdafe442f78b3257ca7fd5981`.
QA branch starts from that exact production commit and changes no production workflow/source.

Independent invariants:
- every external GitHub Action uses a lowercase 40-character commit SHA;
- every checkout explicitly disables credential persistence;
- no workflow pipes a remote HTTP installer into a shell;
- the live Ollama proof binds exact runtime bytes by SHA-256 before extraction;
- the live proof binds exact model bytes from an immutable external commit by SHA-256 before local
  Ollama import;
- no mutable `ollama pull` acquisition is permitted in the live proof.

The security authority is immutable verified bytes before execution/import, not an unproven literal
`ollama pull name@sha256:...` CLI spelling. Engine and model provenance remain separate.

This QA vehicle is independent evidence only. It does not merge into `main` and cannot award
HUMAN_TESTED or NVDA_VERIFIED.
