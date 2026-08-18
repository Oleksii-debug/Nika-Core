# Nika Core — Agent Lab product vision

Updated: 2026-08-18.

## Primary emphasis
Agent Lab is not primarily a chatbot or search wrapper. The product goal is a controlled **digital worker** that can perceive, reason, act, learn from evidence, create specialist subagents and produce durable artifacts.

The user's mental model is intentional: give an agent **eyes, hands, ears, mouth, memory and a brain**.

## Eyes — perception
Prefer structured semantic information before pixels:
1. Web DOM/accessibility semantics.
2. Windows UI Automation/accessibility tree.
3. Files, documents, databases and tool/API responses.
4. Screenshot/image analysis and OCR/vision fallback when semantic interfaces are missing.

An agent must be able to describe to a blind user what an inaccessible site/application exposes visually, while clearly distinguishing structured evidence from vision inference.

## Hands — controlled action
Provide replaceable action adapters:
- browser actions through Playwright/DOM semantics;
- Windows application actions through UI Automation;
- keyboard/mouse coordinate control only as a fallback when semantic control is unavailable;
- files, shell and coding tools in a sandbox;
- APIs and MCP tools;
- versioned reusable skills/plugins.

All actions pass Nika permissions, cancellation and audit. External committing/destructive/high-impact actions require the corresponding approval policy.

## Ears and mouth
- Ears: speech/audio transcription and other audio analysis through local or connected specialist tools.
- Mouth: accessible text reports, optional TTS/speech, notifications and explanations.

These are tools behind ports rather than mandatory monolithic dependencies.

## Brain
Nika supports multiple intelligence levels:
1. Deterministic rules, state machines, planning templates and search/ranking.
2. Classical ML and compact specialist models for classification, pattern recognition and prediction.
3. Vision/audio specialist models where useful.
4. External or local LLM/reasoning providers through Model Gateway when available.

The no-LLM mode must remain useful for known procedures, classification/routing, deterministic automation and trained specialist tasks, but must never be misrepresented as general GPT-level reasoning.

## Memory and learning
Separate task, agent, workspace and user-approved long-term memory. Learning is evidence-driven: corpus reading -> hypotheses/strategies -> simulation/replay -> metrics -> comparison -> controlled promotion/rollback.

For financial/trading/gambling research, the autonomous learning laboratory supports backtesting, paper/demo/simulation and strategy evaluation. Real-money or real-wager execution is never part of the default autonomous path and would require a separate high-risk connector with explicit human approval and additional gates.

## Accessibility Repair Agent
A first-class scenario:
- inspect DOM/UIA/accessibility tree and screenshot when NVDA cannot expose an interface;
- explain the interface to the user;
- navigate/control it under permission;
- propose and build a local adapter/script/overlay/accessibility helper when needed;
- test the helper in isolation;
- store a proven helper as a versioned reusable skill/plugin.

The goal is not to hide inaccessible software behind unexplained coordinate clicks; it is to create a durable accessible interaction layer whenever practical.

## Software Factory workspace
The user can state an end product, e.g. “build an accessible chess application.” Nika should be able to:
1. turn the goal into a product/technical plan and acceptance criteria;
2. research maintained ready-made libraries/code before writing custom infrastructure;
3. create specialist roles/subagents such as architect, researcher, implementer, tester, accessibility reviewer and release worker;
4. use approved external coding/model providers through official APIs/SDKs/CLIs/adapters when available;
5. implement missing code in isolated branches/worktrees;
6. run tests and review evidence;
7. integrate only coherent green changes;
8. build a Windows candidate and present a human NVDA test plan;
9. learn reusable project skills without silently rewriting production source.

Runtime code self-improvement follows sandbox/worktree/branch -> tests -> security -> integration -> release. Runtime agents never overwrite production source directly.

## Architecture consequence
Create a dedicated Computer Interaction Layer with stable ports/adapters. Browser control, Windows UI Automation, vision/OCR, shell/code execution and external tool protocols must be replaceable implementations, not hardwired into Agent Lab business logic.

This vision is a product requirement and should influence roadmap prioritization, acceptance gates, future workspaces and Agent Builder design.