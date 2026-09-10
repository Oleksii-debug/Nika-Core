# Accessible Research Report Exports

Status: implementation candidate for the shared Universal Research artifact layer.

Starting canonical main for this lane: `3fbfabfc93d59183f174ff44098db886cff93bd8`.
The branch must be synchronized with the current live `main` before integration evidence is accepted.

## Scope

`ResearchReportExporter` renders the existing canonical `AccessibleResearchReport` produced by
`ResearchCardService`. It does not introduce a second Research/Corpus result model or review store.
The same report can be rendered as:

- UTF-8 TXT;
- UTF-8 CSV;
- semantic escaped HTML;
- DOCX with real heading hierarchy and explicit text labels;
- XLSX with simple flat sheets, a header row, freeze panes, filters and no merged cells.

The exporter returns bytes, a media type, a generated leaf filename and a SHA-256 digest. It accepts
no output directory or arbitrary filesystem path. Filesystem placement remains the responsibility of a
separate caller with its own path/permission policy.

## REUSE -> ADAPT -> CUSTOM (thin)

- REUSE the current Nika `AccessibleResearchReport`, `ResearchCard`, review state and
  `ResearchEvidence` contracts. No stale DEV01 type is copied from PR #86.
- REUSE the already-declared `python-docx` dependency for DOCX and `openpyxl` for XLSX. Nika does not
  hand-edit Office Open XML as its primary document/spreadsheet implementation.
- ADAPT those libraries only at the artifact boundary to assign stable metadata and accessibility
  structure.
- CUSTOM (thin) only for Nika-owned flattening, provenance/review projection, spreadsheet-injection
  protection, safe filename generation, escaping and deterministic package canonicalization.

No new project dependency is introduced by this lane.

## Safety and provenance invariants

CSV/XLSX string cells whose first non-whitespace character is `=`, `+`, `-` or `@` are emitted as
literal text by prefixing an apostrophe. HTML escapes all public report-controlled text, including titles,
summaries and review notes. Raw source locators are not rendered; public output carries only the canonical
safe evidence-location label. Generated filenames retain only alphanumeric, hyphen and underscore
characters from the result-set identity, are bounded to 64 identity characters, and are returned as a
leaf filename rather than a caller-selected path.

Structured exports preserve result-set/workspace/query identity, result ordering and document identity,
review state/note/update timestamp, ranking/match explanation, source identity/kind/freshness, the public
safe location label rather than the raw locator, and observation timestamp. Empty evidence is represented
explicitly rather than silently inventing a source.

XLSX export also enforces Excel's 32,767-character cell limit before serialization. The check runs
after spreadsheet-formula neutralization, because the protective apostrophe can itself push a boundary
value over the Excel limit. Oversized XLSX fields fail closed with the offending field name; they are
never silently truncated by openpyxl. Other report formats remain independently available.

## Deterministic Office artifacts

`python-docx` and `openpyxl` correctly own the Office document model, but ordinary saves can include
run-time ZIP/member and core-property timestamps. After library serialization, the exporter performs a
thin deterministic package canonicalization step:

1. reject duplicate ZIP member names;
2. order package members by name;
3. bind `docProps/core.xml` created/modified timestamps to the canonical report `created_at`;
4. use a fixed ZIP member timestamp and deterministic compression settings.

The canonical report timestamp must therefore be valid timezone-aware ISO-8601 for DOCX/XLSX output.
Timezone-naive timestamps are rejected instead of being mislabeled as UTC. Focused tests require
byte-for-byte equality for repeated rendering of the same report.

## Accessibility structure

HTML uses a main landmark, heading hierarchy, labelled article sections, definition lists and ordered
evidence lists. HTML export requires the caller to provide an explicit simple BCP47 language tag
(for example `uk` or `en-US`) because the canonical Research report currently has no trustworthy
language metadata. The exporter never guesses with an LLM and no longer emits `lang="und"`.
Exporter-owned English labels are explicitly marked `lang="en"` when the report's default language is
not English. DOCX uses Heading 1 for the report, Heading 2 for results and Heading 3 for evidence;
field labels are explicit bold text rather than color or layout alone. XLSX uses separate `Metadata` and
`Results` sheets, simple headers, no merged cells, freeze panes and auto-filters.

These are automated semantic-structure properties only. They do not award `HUMAN_TESTED` or
`NVDA_VERIFIED`.

## Packaging and compliance truth

At the time this lane was created, the Windows PyInstaller entrypoint `scripts/nika_windows.py` did not
import the Research exporter and the build plan did not declare a hidden import for it. Therefore this
branch does **not** claim that DOCX/XLSX export is present in the packaged Windows user journey.

For the same reason this lane does not add `python-docx`, `openpyxl` or their transitives to the current
`RUNTIME_DISTRIBUTIONS` notice list merely because they exist in `pyproject.toml`; doing so before they
are actually part of the frozen runtime would create orphan notice entries. When report export becomes
reachable from the packaged application, packaging/PF10 integration must correlate the exact frozen
runtime distributions with source/version/license/provenance and required notices before release.

No legal compatibility conclusion is created by this document. Release permission remains owned by the
canonical compliance/review authority and release gates.

## Acceptance evidence required before integration

The lane needs current-main synchronization, focused format tests, dependency consistency, Ruff/compile,
full Core CI and any path-triggered release/dependency/license checks that apply to the exact candidate.
A backend/service export implementation is not a complete packaged Product Journey by itself.

## Public provenance privacy boundary

The canonical accessible text report and every downloadable export use the same public
evidence-locator projection. Raw `ResearchEvidence.locator` is internal provenance and
is never copied into TXT, CSV, HTML, DOCX, or XLSX output.

- HTTP evidence renders `http-source`.
- Local-file evidence renders `local-file`.
- Exact correlation remains available through the stable `source_id`, source kind,
  freshness, and observation timestamp already carried by each evidence item.
- URL userinfo, path/query/fragment material, signed parameters, API-key/token values,
  and private absolute filesystem paths therefore do not cross the report/export
  boundary.
- TXT is regenerated from the structured report cards at export time rather than
  trusting arbitrary pre-rendered `report.text`.

This is intentionally a privacy-minimizing public projection. It does not mutate the
stored research source, source identity, or internal locator.
