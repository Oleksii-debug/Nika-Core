# TECH02 workflow supply-chain convergence

Date: 2026-08-24.
Lane: TECH02 / release-security shared workflow repair.
Starting main: `23c7c1ce97b263b4aafa61bdcbace207b4476a3d`.

## Finding

Independent QA-only PR #378 correctly identified four current-workflow supply-chain gaps:
mutable external Action tags, persisted checkout credentials, a mutable remote Ollama installer piped
directly to a shell, and a mutable model-name/tag download in the live Ollama proof.

This batch repairs only the shared CI/proof boundary. It does not change Product Factory, DEV05 media,
PF7 credential, ModelGateway, packaging runtime, permission, approval, or production model-selection
semantics.

## REUSE -> ADAPT -> CUSTOM(thin)

- REUSE GitHub Actions with exact commit SHAs already proven in Core/M11/M12.
- REUSE Ollama's official versioned Linux release artifact and documented local GGUF import path.
- REUSE the existing 135M SmolLM2 proof model family, but bind it to exact external bytes rather than
  an Ollama registry tag.
- ADAPT the M4 CI proof to checksum-verified artifacts and local `ollama create`.
- CUSTOM(thin): repository policy regression over workflow text only.

No new runtime dependency is added to Nika and no model is added to the Windows package.

## Exact CI-only provenance

Ollama engine:
- version: `0.32.14`;
- source/release project: `ollama/ollama`;
- Linux amd64 release asset: `ollama-linux-amd64.tar.zst`;
- SHA-256: `c620917a71e146ab3a7f893084f066069c4c65d144ef8379a91c3cbe8b27de8f`;
- acquisition: exact versioned GitHub Release URL followed by local SHA-256 verification before
  extraction.

Proof model:
- repository: `bartowski/SmolLM2-135M-Instruct-GGUF`;
- exact commit: `be21a1bc2b344d5b57381053d1dc0faea5f4e40c`;
- file: `SmolLM2-135M-Instruct-Q5_K_M.gguf`;
- SHA-256: `731d0c9cf598dada9712242ceddcca88aa0502fc8f9b8f773917df9f9113463a`;
- size: approximately 112 MB;
- model license reported by the repository: Apache-2.0;
- acquisition: immutable commit-qualified Hugging Face URL plus local SHA-256 verification;
- import: Ollama's documented `Modelfile FROM /path/to/file.gguf` + `ollama create` path.

The engine artifact and model artifact are separate identities and licenses. Neither is silently
installed or downloaded by the shipped product; this is a bounded hosted-CI live-provider proof.

## Security invariants

- every external GitHub Action reference is a full lowercase 40-character commit SHA;
- every `actions/checkout` sets `persist-credentials: false`;
- no workflow pipes a remote HTTP installer directly to `sh` or `bash`;
- the M4 proof performs no `ollama pull` from a mutable model tag;
- both Ollama runtime and model bytes are SHA-256 checked before execution/import;
- the existing ModelGateway live Ollama call remains the behavioral acceptance boundary.

## Acceptance truth

This source change is not GREEN until its exact final head passes Core CI, applicable M11, complete
M12, the live M4 Ollama proof, and independent replay of #378. Repository `main` protection remains a
separate live-GitHub governance blocker and is not claimed repaired by workflow source.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
`PRODUCTION_RELEASE_READY=false`
