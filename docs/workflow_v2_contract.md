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
| 3 | `creative_brief` | 決定成片方向與創作意圖 | Recommend output direction, target duration, pacing, high-level story intent, aspect, resolution, and framing from source geometry/project context | Required: accept recommendation or save human decisions; visual and Story-relevant fields remain separately classified | approved Creative Brief, recommendation provenance |
| 4 | `story_draft` | 產生第一版剪輯結果 | Generate Story from the current StoryInput snapshot and approved Story-relevant brief fields; build a valid Story/storyboard draft and automatically request a cheap Draft Preview | No approval yet; user enters review looking at the playable result | Story generation, storyboard draft, exact segment coverage, Draft Preview |
| 5 | `review` | 看結果並微調 | Play the Draft Preview first; on demand open Timeline, Visual Style, Subtitle, Audio, Color, and other Workspace previews | Required once: confirm the reviewed edit, or return to edit; confirmation starts the Apply boundary operation | candidate semantic drafts, preview evidence, domain readiness |
| 6 | `approval` | 套用並核准目前版本 | Atomically Apply the reviewed candidate when needed, verify the exact applied revision/currentity, then validate provenance, manifest, rights, and all approval contracts | Human intent was captured by Review confirmation; an explicit separate approval action is allowed only if product policy requires it | applied authoritative revision, immutable approval snapshot, approval readiness |
| 7 | `formal_render` | 產生正式成片 | Formal Render Job using approved snapshot only | None while running; cancellation is not success | Render Report, output fingerprint, encoder/probe audit |
| 8 | `automated_qa` | 驗證交付品質 | Final QC and Delivery QA as separate formal gates | Resolve `qa_needs_review` findings; no threshold bypass | QC/QA evidence and gate result |
| 9 | `human_preview` | 最後看成品 | Present the exact final MP4 and evidence bundle | Required: human final preview confirmation | human confirmation bound to render/QA/output fingerprint |
| 10 | `delivered` | 可交付 | Set only after every preceding gate is current and human preview is confirmed | None after confirmation | `deliverable_ready=true` |

### 2.1 Stage transitions

The orchestrator may start independent automatic work, but it must not skip a
required checkpoint:

```text
import → understanding → creative_brief → story_draft → review
review → apply_candidate → applied_revision → approval
approval → formal_render → automated_qa → human_preview → delivered
```

`Story generation` produces a candidate Story generation and draft storyboard;
it does not become the current authoritative project edit. After Review
confirmation, VID-16 Apply validates candidate currentity and atomically commits
storyboard/project revision. Only after exact Apply success may the system
persist or refresh the formal Approval candidate. Apply and Approval are two
authoritative contracts even when one Simple-first CTA orchestrates them
sequentially.

`formal_render` is never entered from a draft, recommendation, preview, or
legacy UI flag. It requires the existing approval gate and an immutable
approval snapshot. `delivered` is never inferred from a successful Render Job;
it requires Final QC, Delivery QA, and an explicit human preview confirmation.

### 2.2 Workflow is a derived projection

The workflow is not an independently writable state machine. The authoritative
state remains in the existing domain contracts:

- source/import and media identity;
- Perception, transcription, and audio analysis runs;
- Creative Brief and its human approval;
- Story generation, storyboard, and Apply revision;
- Visual Style, color, Audio, and Caption Track contracts;
- Approval snapshot;
- Render Job/Render Report, Final QC, Delivery QA, and human final preview.

The following values are deterministic projections of those contracts:

- `stage` / `main_stage`;
- domain `readiness`;
- `next_action`;
- `blocked_reason`.

No API may persist a free-standing value such as
`workflow.current_stage = "review"` and use it to advance the product. If a
future optimization materializes a projection, it must include
`projection_version`, the source state identities/hashes, and `computed_at`.
It must be deterministically rebuildable; source mismatch requires recompute or
fail-closed behavior. A projection can never authorize Approval or Render and
can never overwrite domain state.

### 2.3 Non-linear readiness

The main stage is a summary, not a single progress counter. Review can expose
independent domain readiness, for example:

```yaml
main_stage: review
readiness:
  story: ready
  visual_style: ready
  subtitle: needs_review
  audio: ready
  timeline: needs_review
  approval: blocked
next_action:
  id: review_subtitle
  label: 字幕尚未確認
```

The next action is derived from required blockers and the highest-value pending
decision. Adding a Workspace must not require renumbering a linear stage
counter.

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
Creative Brief checkpoint has approved Story-relevant fields. At minimum these
are bounded target duration, pacing, high-level story intent, and existing
schema-declared Story instructions. Output direction/aspect/resolution/framing
may be confirmed at the same Simple-first checkpoint, but are visual/render
semantics and are excluded from StoryInput identity. Visual Style selection may
remain in the later Review stage because it is primarily visual/render semantics
and must not unnecessarily block Story generation. Story generation continues
to use its existing context preflight,
strict schema, semantic validation, compact corrective retry, and fail-closed
publish gate.

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

### 4.1 Horizontal extensibility: WorkflowDescriptor

Workflow v2 is horizontally extensible in stages, Workspaces, analysis jobs,
review capabilities, human checkpoints, and preview/render tiers. This is a
small descriptor contract, not a universal plugin framework.

Each registered descriptor must provide:

```yaml
id: subtitle
version: 1
label: 字幕
owner_domain: caption_track
dependencies:
  - semantic_state_identity: transcript_id/revision
readiness_resolver_identity: caption_track_readiness_v1
requiredness: conditional # required | optional | conditional
surface_role: workspace # main_path | workspace | drill_down | diagnostic
invalidation_class: visual_render_only
capability_requirement: local_caption_editor_v1
next_action_identity: review_subtitle
order: 60
```

The descriptor also defines dependency ordering and unknown/unavailable
semantics. Unknown descriptor or version is unsupported and fails closed.
Missing required dependency produces a blocked readiness with a human-readable
reason. An unavailable optional Workspace does not block the main path. A
required unavailable capability blocks the relevant checkpoint.

For example, a future `sound_design` Workspace can register its owner domain,
audio dependency, readiness resolver, disclosure role, and review action. The
Workflow projection can then surface it without adding a special branch for
`stage === "sound_design"` to every existing consumer. This round defines only
the contract/example; it does not implement descriptor loading or a plugin
runtime.

## 5. Semantic state ownership

| Semantic state | Authoritative owner | Consumers |
| --- | --- | --- |
| Source path, fingerprint, coded/display geometry | source/media records and approved source evidence | Perception, StoryInput, Render, QA |
| Perception windows/results and provider provenance | Perception run store | StoryInput, understanding UI, audit |
| Source Transcript and word timestamps | Source Transcript artifact contract | StoryInput, Caption Track derivation, search/audit |
| Caption Track cues and style | Caption Track editing contract | Subtitle Workspace, preview, Render, QA |
| Creative Intent recommendation | Creative Brief recommendation state | checkpoint UI, Story context |
| Approved output direction/framing | persisted approved Creative Brief snapshot | Draft Preview, Visual Style, Render, VID-27 |
| Story-relevant Creative Intent | persisted Creative Brief fields included in StoryInput | Story generation / StoryInput |
| Candidate Story generation | Story generation store/cache contract | Story review, Draft Preview, Apply |
| Storyboard order/timing/segment inclusion | storyboard state and Apply revision | Draft Preview, approval, Render |
| Visual style/title/grading | Visual Style and color contracts | preview, manifest, Render |
| Audio role/BGM/normalization | Audio state and BGM provenance | preview, manifest, Render, QA |
| Subtitle cues/style | Subtitle Workspace contract | preview, Render, QA |
| Approval | immutable approval snapshot | Render, Render Report, Delivery QA |
| Render result | Render Job/Render Report | Final QC, Delivery QA, preview |
| Delivery readiness | Delivery QA plus human preview confirmation | final delivery state |

No stage may reconstruct these values from labels, browser state, stale cached
payloads, or another Workspace's local draft.

The following identities are intentionally distinct:

- **Story Generation Store**: candidate AI generation;
- **Storyboard / Apply revision**: current authoritative edit state;
- **Draft Preview**: candidate evidence bound to candidate Story, draft
  storyboard, visual semantics, and source identity;
- **Approval Snapshot**: exact applied revision approval;
- **Formal Render**: consumer of the Approval Snapshot.

A Story generation ID or Draft Preview ID alone can never authorize Approval or
Formal Render.

## 6. Creative Brief checkpoint

The checkpoint is before Story generation and is the first creative decision in
the workflow. Its Simple-first surface must expose only the high-value
Creative Intent decisions, while preserving their separate semantic classes:

- output direction: 16:9 or 9:16;
- target duration: AI recommendation plus bounded presets/human override, such
  as approximately 60 seconds, 90 seconds, or 2 minutes;
- pacing: AI recommendation plus registry-backed `relaxed`, `natural`, or
  `fast` semantics;
- high-level story intent: one concise human-editable request, such as
  「以手沖過程為主，保留準備與收尾」.

The surface must not expose `must_keep`, `exclude`, provider settings, contract
hashes, or every Story knob in the primary path. Those belong in Advanced or a
later Workspace. The approved fields consumed by Story are persisted in the
existing Creative Brief contract; the UI does not create a second intent store.

It must also display:

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

The checkpoint intentionally presents two semantic classes together for a
simple decision, but persists them separately:

**Visual/render-only Creative Brief fields**

- output direction;
- aspect ratio and resolution;
- framing strategy.

These can be confirmed before Story so Draft Preview and geometry know the
target, but they are excluded from Story-relevant invalidation identity. A
16:9 → 9:16 change stales the visual Creative Brief contract, Visual Style
preview, Draft Preview pixels, approval snapshot, and downstream render/cache
identity; it does not rerun Perception or Story.

**Story-relevant Creative Intent fields**

- bounded target duration;
- pacing;
- high-level Story intent;
- `must_keep`, `exclude`, desired sequence, user summary, and other
  schema-declared Story instructions.

These are the fields consumed by StoryInput. Changing them stales Story and
the Draft Preview and requires Story regeneration.

Visual Style choices (`Diary Natural`, `Clean Minimal`, `Cinematic`) remain in
the Story draft Review stage. They are primarily visual/render semantics and do
not block Story generation unless a future schema explicitly marks a field as
Story-relevant.

## 7. Review surface and preview tiers

Stage 5 is where users first see a result before paying the cost of formal
Render. After an approved Creative Brief, valid Story/storyboard draft, and
exact segment coverage, the product automatically requests a **Draft / Rough-cut
Preview**. The user should see 「AI 已經幫你剪了一版」 and a playable result
before seeing Story JSON or opening technical controls.

The tiers are intentionally separate:

| Tier | Authority | Cost/source state | Approval? | Deliverable? |
| --- | --- | --- | --- | --- |
| Story Draft | semantic editing draft | Story/storyboard draft; no encoded final output | No | No |
| Draft / Rough-cut Preview | cheap playable evidence | proxy/cache/local preview path from current draft | No | No |
| Workspace Preview | local evidence for one domain | current pending Visual Style, Subtitle, Audio, Color, Timeline, or range state | No | No |
| Formal Render | approved final render | immutable approved snapshot and current source evidence | Produces candidate only | No |
| Automated QA | authoritative Final QC + Delivery QA | exact formal output and provenance | Gate required | No |
| Human Final Preview | exact output review | exact formal MP4 plus current QA evidence | Final human gate | No until confirmed |
| Delivered | delivery state | all gates current and confirmed | Already passed | `deliverable_ready=true` |

Draft Preview is playable, cheap, reversible, and may become stale after
pending review edits. Its identity is bound to the candidate Story generation,
relevant storyboard draft identity, visual-only Creative Brief contract,
current preview-relevant visual semantics, and source identity. A
visual-direction/framing change therefore stales the Draft Preview and may
regenerate it cheaply while reusing the same Story/storyboard candidate. A
Story-relevant intent change stales both Story and Draft Preview. Pure
disclosure changes stale nothing. Draft Preview does not imply Apply, Formal
Render, QC PASS, Approval, or delivery. This round defines the contract only;
it does not implement a Draft Preview renderer.

The primary Review actions are:

- `這版方向可以` → enter the single Review completed/Approve edit checkpoint;
- `微調` → disclose Timeline, Visual Style, Subtitle, Audio, Color, or other
  Workspace on demand.

Preview success never implies approval, Render success, QA success, or
`deliverable_ready`. Preview cache identities remain artifact identities;
approval and semantic provenance remain separate identities.

## 8. Subtitle Workspace position and contract

Subtitle is a Stage 5 editing Workspace after transcription is available and
before approval. It is not a mandatory early form field and it is not silently
burned into a draft merely because transcription exists.

### 8.1 Source Transcript

Source Transcript is the authoritative speech-understanding artifact. Its
minimum identity is:

- `transcript_id` and `revision`;
- language;
- words and word timestamps;
- provider/model provenance.

Its primary consumers are StoryInput, Caption Track derivation, search, and
audit. A new transcript revision is a new Story-relevant identity when the
current StoryInput includes the transcript identity; it then makes the current
Story stale according to the existing fail-closed currentity rules.

### 8.2 Caption Track

Caption Track is derived from Source Transcript but is an independent editing
semantic layer. Its minimum identity is:

- `caption_track_id` and `revision`;
- source transcript identity;
- ordered cues with stable cue IDs, text, and timing;
- style references;
- human edit provenance.

The Subtitle Workspace operates on Caption Track by default. Text correction,
Traditional Chinese sentence breaking, proper-noun edits, cue merge/split,
and subtitle timing changes update Caption Track only. They must not silently
rewrite Source Transcript or make Story stale. An explicit user action such as
「修正逐字稿」 creates a new transcript revision instead.

AI output must also identify whether it is a `caption_suggestion` or a
`source_transcript_correction`; a suggestion cannot silently become a source
correction.

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
artifact changes. A direction/framing change stales any Draft Preview whose
pixels depend on that visual contract, but may reuse the same Story/storyboard
candidate and regenerate only the cheap preview. These changes must not force
Perception or Story rerun unless the same mutation also changes a separate
Story-relevant field.

### 9.2 Story-relevant changes

These invalidate the affected StoryInput/generation and downstream Apply,
approval, and Render artifacts:

- source replacement or source fingerprint/geometry change;
- segment inclusion, order, source range, or semantic action/shot evidence;
- user summary, must-keep/exclude, desired sequence, pacing, or other
  human-authored Story instructions;
- target duration, pacing, high-level story intent, or another Creative Brief
  field explicitly declared Story-relevant by its schema when included in
  StoryInput;
- Source Transcript text/timestamps or transcript revision when the transcript
  identity is included in StoryInput.

Caption Track text/timing/style edits alone are visual/render or subtitle
review changes and do not stale Story. An explicit Source Transcript correction
creates a new transcript identity and follows the StoryInput rule above.

### 9.3 Formal approval currentity

Any mutation after approval must result in either an exact current approval
snapshot or a new `needs_review`/stale state. No API may silently reuse a
previous approval because the visible UI appears equivalent. The existing
stale-before-Apply and post-Apply historical-generation semantics remain
unchanged.

## 10. Human checkpoints

The normal path converges human decisions into three primary checkpoints:

### A. Creative Intent

Before Story generation, the user accepts or overrides output direction, target
duration, pacing, and high-level story intent. This is the Creative Brief
checkpoint.

### B. Review completed / Approve edit

After the playable Draft Preview, the user either accepts the direction or opens
the relevant Workspace to make edits. Visual Style, Subtitle, Audio, Color,
and Timeline remain domain-level persisted currentity, but they do not become
separate compulsory approval pages in the main path. The user confirms the
reviewed revision once, after required domain readiness is current. The single
CTA may say `確認這個版本`; it need not expose the engineering term Apply.

The backend orchestration is nevertheless sequential and fail-closed:

```text
human review intent
  → VID-16 Apply candidate
  → verify exact applied project revision/currentity
  → persist formal Approval
```

VID-16 Apply must validate the candidate Story generation, validate stale
currentity, atomically commit storyboard/project revision, return the exact
applied revision, and leave no partial success on failure. Approval must bind
that exact applied revision, not a Story generation ID, Draft Preview ID, or
browser draft. Apply failure stops before Approval; Approval validation failure
leaves the applied revision authoritative and marks Approval `needs_review` or
`blocked` rather than rolling back or pretending Apply did not happen.

### C. Human Final Preview

After Formal Render, Final QC, and Delivery QA, the user confirms the exact
output. `qa_needs_review` creates a conditional checkpoint only when warnings
require human resolution; it is not an automatic pass.

## 11. Failure and recovery semantics

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

### 11.1 Explicit failure cases

**Case A — Apply stale/currentity failure**

The user confirms a current Draft Preview, but Apply detects a stale candidate:

```yaml
main_stage: review
approval: blocked
formal_render: unavailable
```

No Approval call is made; the user returns to Review.

**Case B — Apply succeeds, Approval validation fails**

Apply commits project revision 9, but formal Approval validation fails. Revision
9 remains the authoritative project state; Approval is `needs_review` or
`blocked`, and Formal Render is unavailable. The system does not roll back
revision 9.

**Case C — Visual direction changes after Story**

Story generation remains current. The visual Creative Brief, Visual Style
preview, and Draft Preview become stale; the same Story/storyboard candidate
may regenerate a cheap Draft Preview without rerunning Story.

**Case D — Target duration changes after Story**

StoryInput and Story generation become stale. Draft Preview, Apply, and Approval
are unavailable until Story is regenerated.

## 12. Existing VID scope map

| VID | Treatment | Workflow v2 stage/workspace | Dependency and timing |
| --- | --- | --- | --- |
| VID-11 | retain | acceptance / hardening umbrella | Remains active until VID-18 Round-1 closure; Workflow v2 cannot skip it. |
| VID-18 | retain | formal Render → QA → human preview | Current Round-1 deliverable acceptance remains independent and must close before delivery claims. |
| VID-25 | reposition; eventually superseded as UX v1 by VID-41 slices | Project orchestrator / Review shell | Preserve completed child results; migrate only after this contract is reviewed. |
| VID-26 | retain | Creative Brief / Creative Intent checkpoint | Mixed domain: Story-relevant duration/pacing/high-level Story intent/instructions; visual-only direction/aspect/resolution/framing. |
| VID-27 | retain | Visual Style / Workspace Preview | Review drill-down after Draft Preview; consumes approved Creative Brief. |
| VID-28 | retain; reposition | Timeline Inspector drill-down in Review | Do after the Workflow shell placement is stable; not required to define this contract. |
| VID-29 | defer | Editorial Policy domain | Start after Workflow v2 core flow is stable; no policy engine in this round. |
| VID-16 | retain | Review → Apply → Approval boundary | Atomic Apply remains authoritative; Workflow v2 does not replace it. A single CTA may orchestrate Apply then Approval sequentially, fail-closed. |
| VID-35 | retain backlog | Render performance / formal Render | Independent performance work; does not block Workflow v2 UX. |
| VID-40 | completed; retain principles | Simple-first components / progressive disclosure | Reuse locally; it is not the whole Workflow v2 orchestrator. |
| VID-41 | active | Workflow v2 control-plane contract | This document is Round 1; implementation follows focused slices. |
| VID-42 | retain; do not duplicate | Subtitle Workspace in Review | Caption Track editor after transcription; implement as a focused follow-up. |

This is a contract-level migration/remap plan only. It does not change Linear
relations or statuses and does not create the follow-up issues.

## 13. Primary UX example: Coffee project

This example is the acceptance lens for the contract; the experience must not
devolve into a pipeline control panel.

1. The user drags Coffee source clips into a new project.
2. The project shows 「AI 正在整理 17 個片段」 while Perception,
   transcription, audio analysis, and source geometry run in the background.
3. Once the required evidence is ready, the simple Creative Intent checkpoint
   shows:

   ```text
   建議：16:9 · 約 90 秒 · 自然節奏
   [採用建議並產生第一版]    詳細設定 ▾
   ```

   The user may edit the direction, duration, pacing, or one-sentence story
   intent before accepting it.
4. The system generates Story and a cheap playable Draft Preview. The user
   sees 「第一版完成」 and can press [播放]. The first result is the preview,
   not Story JSON or a wall of technical controls.
5. If the direction is good, the user presses [這版方向可以]. If not, [微調]
   opens Timeline, Visual Style, Subtitle, Audio, or Color only as needed.
6. The user presses [確認這個版本] once after required Review readiness is
   current. The UI may show 「正在套用這個版本」 while the system performs
   the boundary operation; it need not expose the word Apply.
7. The system atomically Applies the candidate and verifies the exact applied
   project revision.
8. Only after Apply succeeds does the system persist formal Approval.
9. The product runs Formal Render, Final QC, and Delivery QA.
10. The user performs the final human preview of the exact MP4.
11. Only then does the system set `deliverable_ready=true`.

For a user who accepts the recommendation and makes no edits, the minimum
human decisions are Creative Intent, Review completed/Approve edit, and Human
Final Preview. Conditional QA review may add a decision only when warnings
require it.

## 14. Implementation boundary after this contract

This contract intentionally does not implement the full product reorganization.
The next implementation issues should be narrow and independently verifiable:

1. derived workflow readiness / next-action projection API, rebuilt from
   authoritative domain identities rather than independently persisted workflow
   state;
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

## 15. Acceptance checklist for VID-41 Round 1

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
- [x] The first playable Draft Preview is explicitly positioned before detailed
  Workspace editing.
- [x] Story-before intent covers direction, duration, pacing, and high-level
  story intent without exposing every Story knob.
- [x] Source Transcript and Caption Track ownership are separated.
- [x] WorkflowDescriptor extensibility and non-linear readiness are defined.
- [x] The Coffee user journey and minimum human decisions are explicit.

The contract answers the required correctness questions unambiguously:

- 16:9 → 9:16: no Story rerun; only visual/Draft Preview/downstream render
  identities become stale.
- 90 seconds → 60 seconds: StoryInput and Story become stale; regeneration is
  required.
- AI Story generation: candidate only, not the current authoritative project
  edit; VID-16 Apply is required.
- Draft Preview: cannot enter Formal Render directly; Review → Apply → formal
  Approval is required.
- One-click `確認這個版本`: allowed as UX, but the backend must execute Apply
  → exact applied revision → Approval sequentially and fail closed.
- Apply failure: Approval must not be created or updated as successful.
