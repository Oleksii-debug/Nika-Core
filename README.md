# Nika Core

Accessible modular Windows multi-agent platform with autonomous agents, memory, controlled learning loops, model gateway and NVDA-first interface.

Status: ACTIVE DEVELOPMENT. Canonical progress and next gate: `state/PROJECT_STATUS.md` and the GitHub LIVE DASHBOARD issue.

Start by reading `AGENTS.md`, `docs/MASTER_SPEC.md`, `docs/ROADMAP.md`, and `docs/THIRD_PARTY_ADOPTION.md`.

Binding future product architecture also includes a commercial Web/Cloud edition and optional local Windows execution node. Read `docs/WEB_CLOUD_PRODUCT_ARCHITECTURE.md`. This does **not** replace or delay the current Windows/NVDA release path; it defines separation rules so current development does not close the future web path.

Development is Python-first for fast iteration. Windows standalone `.exe`/ZIP candidates are built at milestone/user/release gates; the final product must run without Python installed.

## Reproducible development verification

Create/activate a Python 3.12 virtual environment, install the milestone extras, then use the same verification harness locally and in GitHub CI:

```bash
python -m pip install -e ".[dev]"
python scripts/verify.py
```

The harness checks installed dependency consistency, Ruff, Python compilation and the complete pytest suite. Runtime milestones that add optional integrations install the relevant extras first (for example `.[dev,agent]`) and then run the same harness. A prepared test file is not counted as passing until this command actually executes successfully on the exact candidate SHA.
