# Video Vault AI Workflow v2 Contract

Status: design contract for VID-41 Round 1

Contract version: `editor-workflow-v2-v1`

This document defines the product workflow and state ownership before engine or
large-scale UI changes are made. It orchestrates the existing persisted
Perception, Story, Creative Brief, Visual Style, Audio, Render, QC, Approval,
Provenance, and Delivery QA contracts. It does not replace those contracts and
does not grant approval automatically.

## 1. Product outcome

The primary path is result-first and project-oriented:

```text
素材匯入
  → AI 理解與整理
  → Creative Brief checkpoint
  → AI Story / rough cut
  → 人工看結果並微調
  → 預覽驗證
  → 核准
  → 正式 Render / QC / Delivery QA
  → 人工 final preview
```

The normal path exposes only the next meaningful decision. Technical evidence,
diagnostics, and advanced controls remain available through progressive
disclosure or a dedicated Workspace. The UI must never create a second
UI-only semantic source of truth.

## 2. Workflow stages

`workflow.stage_contract_version` is `editor-workflow-v2-v1`. Stage IDs are
stable API identifiers; labels may be localized.

| Order | Stage ID | User-facing purpose | Automatic work | Human checkpoint | Produced state/artifacts |
| --- | --- | --- | --- | --- | --- |
| 1 | `import` | 匯入素材 | Copy/reference source assets, fingerprint, probe metadata | None; user starts import | immutable source identity, clip records |
| 2 | `understanding` | AI 理解與整理 | Perception, transcription, audio perception, summaries; independent jobs may run in parallel after import | Review only when an analysis is blocked or needs correction | current perception runs, transcript, audio candidates, source/orientation summary |
| 3 | `creative_brief` | 決定成片方向 | Recommend output orientation, aspect, resolution, and framing intent from source geometry/project context | Required: accept recommendation or save a human override | approved Creative Brief, recommendation provenance |
| 4 | `story_draft` | 產生第一版剪輯結果 | Generate Story from the current StoryInput snapshot and available approved brief context; build rough-cut/storyboard draft | No approval yet; user enters review when draft is ready | Story generation, storyboard draft, exact segment coverage |
| 5 | `review` | 看結果並微調 | Cheap local previews, representative frames, chapter/segment previews, validation | Required: edit/review Story, timing, style, audio, color, title, subtitle as needed | pending semantic drafts and preview evidence |
| 6 | `approval` | 核准目前版本 | Validate currentity, provenance, manifest, rights, and all required contracts | Required: approve the exact reviewed revision | immutable approval snapshot and approved revision |
| 7 | `formal_render` | 產生正式成片 | Formal Render Job using approved snapshot only | None while running; cancellation is not success | Render Report, output fingerprint, encoder/probe audit |
| 8 | `automated_qa` | 驗證交付品質 | Final QC and Delivery QA as separate formal gates | Resolve `qa_needs_review` findings; no threshold bypass | QC/QA evidence and gate result |
| 9 | `human_preview` | 最後看成品 | Present the exact final MP4 and evidence bundle | Required: human final preview confirmation | human confirmation bound to render/QA/output fingerprint |
| 10 | `delivered` | 可交付 | Set only after every preceding gate is current and human preview is confirmed | None after confirmation | `deliverable_ready=true` |

### 2.1 Stage transitions

The orchestrator may start independent automatic work, but it must not skip a
required checkpoint:

```text
import → understanding → creative_brief → story_draft → review
review → approval → formal_render → automated_qa → human_preview → delivered
```

`formal_render` is never entered from a draft, recommendation, preview, or
legacy UI flag. It requires the existing approval gate and an immutable
approval snapshot. `delivered` is never inferred from a successful Render Job;
it requires Final QC, Delivery QA, and an explicit human preview confirmation.

## 3. Automatic work and concurrency

After `import` has persisted source identity, the following understanding jobs
may run independently or in parallel:

- visual Perception and multi-frame evidence;
- transcription and word timestamps;
- audio perception and waveform analysis;
- source geometry/orientation summary.

Each job owns its own persisted run, provider/model audit, input identity, and
failure state. A failed or unavailable analysis is not silently represented as
success. The `understanding` stage is complete only when the required project
policy says the available evidence is sufficient; the UI must show partial or
blocked analysis explicitly.

Story generation starts only after its StoryInput snapshot is complete and the
Creative Brief checkpoint has an approved output contract. Story generation
continues to use its existing context preflight, strict schema, semantic
validation, compact corrective retry, and fail-closed publish gate.

## 4. Workspace information architecture

The Project page is the orchestrator and status surface. It should show the
current stage, the next action, a compact result summary, and a link to the
relevant Workspace. It must not render every technical control at once.

| Workspace | Primary responsibility | Appears in main path |
| --- | --- | --- |
| `understanding` | Source summary, perception/transcription/audio progress, blocked evidence | Stage 2 drill-down |
| `creative_brief` | Output direction, framing intent, recommendation vs human approval | Required checkpoint before Story |
| `story` | Story generation history, result summary, chapter/segment coverage, review entry | Stage 4–5 |
| `timeline` | Segment order, timing, chapter boundaries, storyboard inspection | Stage 5 drill-down; VID-28 remains separate |
| `visual_style` | Style, grading/LUT, title controls, real preview evidence | Stage 5 drill-down |
| `subtitle` | Transcript/cue editing and subtitle preview | Stage 5 drill-down |
| `audio` | Original audio role, BGM, fades, normalization, audio preview | Stage 5 drill-down |
| `color` | Color analysis and approved grading/LUT semantics | Stage 5 drill-down |
| `approval` | Currentity, provenance, rights, review notes, approval action | Required Stage 6 |
| `render` | Render Job progress and report | Stage 7 |
| `delivery_qa` | Formal QA evidence and review findings | Stage 8 |

The main path may link to a Workspace more than once, but each Workspace must
read and write the existing persisted semantic contract for its domain. A
Workspace-local draft is temporary UI state until its domain save API commits it.

## 5. Semantic state ownership

| Semantic state | Authoritative owner | Consumers |
| --- | --- | --- |
| Source path, fingerprint, coded/display geometry | source/media records and approved source evidence | Perception, StoryInput, Render, QA |
| Perception windows/results and provider provenance | Perception run store | StoryInput, understanding UI, audit |
| Transcript and word timestamps | transcription artifact contract | Story, Subtitle Workspace, Render |
| Creative Brief recommendation | Creative Brief recommendation state | checkpoint UI, Story context |
| Approved output direction/framing | persisted approved Creative Brief snapshot | Story context, Render, VID-27 |
| Story input and generation | Story generation store/cache contract | Story review, Apply |
| Storyboard order/timing/segment inclusion | storyboard state and Apply revision | preview, approval, Render |
| Visual style/title/grading | Visual Style and color contracts | preview, manifest, Render |
| Audio role/BGM/normalization | Audio state and BGM provenance | preview, manifest, Render, QA |
| Subtitle cues/style | Subtitle Workspace contract | preview, Render, QA |
| Approval | immutable approval snapshot | Render, Render Report, Delivery QA |
| Render result | Render Job/Render Report | Final QC, Delivery QA, preview |
| Delivery readiness | Delivery QA plus human preview confirmation | final delivery state |

No stage may reconstruct these values from labels, browser state, stale cached
payloads, or another Workspace's local draft.

## 6. Creative Brief checkpoint

The checkpoint is before Story generation and is the first creative decision in
the workflow. It must display:

- source orientation summary using VID-39 normalized display geometry;
- recommended output contract and reason;
- human-selectable output contract, currently landscape 16:9 and portrait
  9:16 through the existing registry;
- resolution and framing intent for portrait-source-in-landscape and
  landscape-source-in-portrait;
- a clear distinction between recommendation and approved value.

The user may accept the recommendation or save an override. Saving an approved
brief is an explicit human action. Migration may create a deterministic
recommendation with `needs_confirmation`, but must never create human approval.

## 7. Review surface and preview layers

Stage 5 is where users see a result before paying the cost of formal Render.
The layers are intentionally separate:

1. **Draft result**: Story/storyboard structure and coverage.
2. **Local preview**: segment, transition, range, visual-style, audio, and
   subtitle previews using the existing preview contracts and cache identity.
3. **Formal Render**: approved snapshot only; produces the final encoded MP4.
4. **Final QC / Delivery QA**: authoritative automated gates over the formal
   output.
5. **Human final preview**: explicit confirmation over the exact output.

Preview success never implies approval, Render success, QA success, or
`deliverable_ready`. Preview cache identities remain artifact identities;
approval and semantic provenance remain separate identities.

## 8. Subtitle Workspace position and contract

Subtitle is a Stage 5 editing Workspace after transcription is available and
before approval. It is not a mandatory early form field and it is not silently
burned into a draft merely because transcription exists.

The first contract must support:

- transcript words with stable IDs and timestamps;
- cue list with stable cue IDs, start/end, text, and source word IDs;
- Traditional Chinese sentence breaking as an explicit transformation with
  version/provenance;
- cue search/edit, split, merge, and start/end adjustment;
- undo/redo history for unsaved editing;
- safe-zone and visual style preview;
- font registry entries plus user-imported TTF/OTF provenance;
- AI suggestions for typo, proper noun, and sentence-break corrections.

Subtitle edits are human-authored semantic edits. They must be reversible and
must become part of the approved Render contract only after save, preview, and
approval. WhatSub is a UX reference only; no external code or service is a
runtime dependency.

## 9. Stale and invalidation classes

Workflow v2 distinguishes currentity by semantic impact. A UI navigation or
disclosure change is never an invalidation.

### 9.1 Visual/render-only changes

These invalidate visual preview, approved visual/render identity, or Render
cache as appropriate, but do not make Perception or Story stale by themselves:

- output orientation/aspect/resolution/framing intent;
- Visual Style, title, grading/LUT;
- subtitle style/layout and cue timing after the subtitle contract is part of
  the approved render semantics;
- audio role/volume/fade/BGM/normalization;
- renderer/encoder/GPU execution contracts.

They require a new visual preview or approval snapshot when the approved
artifact changes, but must not force Perception/Story rerun unless a separate
Story-relevant field changed.

### 9.2 Story-relevant changes

These invalidate the affected StoryInput/generation and downstream Apply,
approval, and Render artifacts:

- source replacement or source fingerprint/geometry change;
- segment inclusion, order, source range, or semantic action/shot evidence;
- user summary, must-keep/exclude, desired sequence, pacing, or other
  human-authored Story instructions;
- a Creative Brief field explicitly declared Story-relevant by its schema;
- transcript text/timestamps when they are included in StoryInput.

### 9.3 Formal approval currentity

Any mutation after approval must result in either an exact current approval
snapshot or a new `needs_review`/stale state. No API may silently reuse a
previous approval because the visible UI appears equivalent. The existing
stale-before-Apply and post-Apply historical-generation semantics remain
unchanged.

## 10. Failure and recovery semantics

- Automatic jobs expose `queued`, `running`, `succeeded`, `blocked`, and
  `failed`; unavailable evidence is never displayed as complete.
- A user can retry an idempotent automatic job only through its product job
  control. Retry must use a new audited attempt and current input identity.
- A failed refresh after a successful mutation must preserve the authoritative
  mutation result and show refresh-pending/failed state; it must not replay the
  mutation.
- Approval, Final QC, Delivery QA, and human preview remain fail-closed.
- Cancelled/interrupted Render is not PASS or FAIL and cannot advance the
  workflow.
- Source media and production data remain immutable during preview and formal
  acceptance work.

## 11. Existing VID scope map

| Existing work | Workflow v2 treatment |
| --- | --- |
| VID-15 / VID-20 | Keep strict multi-frame planning, UUID coverage, rescue provenance, and publish gate inside `understanding`. |
| VID-21–24 / VID-30 | Keep StoryInput budgeting, live context authority, runtime provisioning, strict schema, and compact retry inside `story_draft`. |
| VID-16 | Keep Apply authoritative and atomic; it is a Stage 5→6 boundary operation. |
| VID-17 | Keep Doctor capability evidence as health/readiness evidence, never as formal Render/QC replacement. |
| VID-26 | Own the Creative Brief checkpoint and approved output contract. |
| VID-27 / VID-39 | Own visual style, title, geometry, framing, and real preview behavior in Stage 5. |
| VID-28 | Keep Timeline Inspector as a Stage 5 drill-down until Workflow v2 decides whether to promote it. |
| VID-29 | Keep Editorial Policy out of this contract implementation; it follows after the workflow is stable. |
| VID-31–38 | Preserve source probe, encoder/GPU, approval provenance, cache, QC, and render contracts under formal Render/QA. |
| VID-40 | Reuse Simple-first and progressive-disclosure component principles locally; do not make it the whole workflow orchestrator. |
| VID-41 | Defines this orchestration contract and the implementation slices that follow. |

## 12. Implementation boundary after this contract

This contract intentionally does not implement the full product reorganization.
The next implementation issues should be narrow and independently verifiable:

1. persisted workflow stage/readiness projection and next-action API;
2. Project-page result-first stage shell with Workspace routing;
3. understanding job aggregation and blocked/partial evidence presentation;
4. Creative Brief checkpoint integration before Story generation;
5. Story draft/review handoff and preview entry points;
6. Subtitle Workspace data model and local editing surface;
7. approval/currentity wiring for the new stage boundaries;
8. formal Render → QC → Delivery QA → human preview handoff.

Each implementation issue must identify the existing authoritative contract it
orchestrates, the invalidation class it changes, and the evidence required to
prove that it has not bypassed an existing gate.

## 13. Acceptance checklist for VID-41 Round 1

- [x] Stage order and transition rules are explicit.
- [x] Automatic work and human checkpoints are explicit.
- [x] Workspace information architecture and Subtitle Workspace location are
  explicit.
- [x] Semantic state ownership is explicit; no second UI-only source of truth.
- [x] Stale/invalidation classes are explicit.
- [x] Preview, Draft, Formal Render, QC, Delivery QA, and human preview are
  separated.
- [x] Existing VID tasks are mapped to retain/merge/reorder scope.
- [x] Implementation is deferred to focused follow-up issues rather than a
  single unsafe product rewrite.
