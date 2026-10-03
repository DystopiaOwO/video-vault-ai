# VID-18 E4B formal perception blocker

- baseline `origin/main`: `361e303b707d64799b95e781b8daa21eeacaca72`
- acceptance worktree: `.work/video-vault-ai-acceptance-vid18`
- isolated config: `.work/video-vault-ai-acceptance-vid18-data/vid18.yaml`
- isolated project: `project_id=1`, `VID-18 Coffee Acceptance`
- perception model: `google/gemma-4-e4b`
- Story model configured but not reached: `gemma-4-12b-it`
- formal perception run: `f0ddb926-1992-4103-9ee0-d74f6b30b017`
- requested/started: `2026-08-11T11:10:11Z`
- finished: `2026-08-11T11:20:53Z`

## Completed gates

Doctor `--full` passed its required gates after the locally requested Doctor
probe correction:

- overall: `warning`, `ok=true`
- summary: `24 pass / 2 warning / 0 blocked / 5 skipped`
- local endpoint: loopback `127.0.0.1`
- perception capability: `verified_by_behavior`
- runtime 3-of-5 challenge expected and parsed order: `red, yellow, purple`
- generation POST calls: `1`
- request `reasoning_effort=none`; response reasoning chars: `0`
- cloud fallback / paid call / config mutation / model download: all `false`
- Story context capacity: `unknown`; generation-time preflight remains fail closed

Two real Coffee source clips were copied into the isolated project through the
WebUI import flow. The isolated project revision advanced to `2` and both clips
were registered successfully.

## First authoritative blocker

The formal perception job failed closed on the first clip. It did not publish
a perception revision:

- DB run status: `failed`
- `published_at=null`, `published_revision=null`
- project status: `needs_review`
- clip 1: `analysis_status=failed`, `perception_revision=0`
- clip 2: `analysis_status=uploaded`, `perception_revision=0` (not started)
- extracted frame JPEGs: `57`
- mandatory planned windows: `19`
- locally valid windows: `14`
- skipped windows: `5`
- actual vision calls: `0`
- window results: `0`
- published segments: `0`
- window validation: `blocked`
- multi-frame contract: `blocked`

Skipped mandatory windows:

| ordinal | window UUID | interval | frames | validation |
| ---: | --- | --- | ---: | --- |
| 10 | `window_762c1b1e3b2e0cf0fa699867` | 55.0-60.0s | 2 | `insufficient_evidence_frames` |
| 11 | `window_c02a53c13537a75a58b98535` | 70.0-70.0s | 1 | `insufficient_evidence_frames` |
| 12 | `window_3e6acd66f9a242bc1270927c` | 85.0-95.0s | 2 | `insufficient_evidence_frames` |
| 18 | `window_62195d49ef9da2d1c3edfade` | 162.5-162.5s | 1 | `insufficient_evidence_frames` |
| 19 | `window_89fcb93faf5618133ec1d342` | 163.127656-163.127656s | 1 | `insufficient_evidence_frames` |

Formal failure message:

`multi-frame evidence validation blocked: multi_frame_contract_not_pass, window_validation_not_pass, mandatory_window_window_762c1b1e3b2e0cf0fa699867_validation_not_pass, mandatory_window_window_c02a53c13537a75a58b98535_validation_not_pass, mandatory_window_window_3e6acd66f9a242bc1270927c_validation_not_pass, mandatory_window_window_62195d49ef9da2d1c3edfade_validation_not_pass, mandatory_window_window_89fcb93faf5618133ec1d342_validation_not_pass, missing_evidence_window_results, window_result_coverage_mismatch`

## Root cause confirmed from the production path

There are two independent blocking contracts:

1. Doctor behavior verification is diagnostic only and is not persisted or
   injected as the perception provider's auditable `multi_frame_capability`.
   `vid18.yaml` has no `ai.local.multi_frame_capability`; consequently
   `provider_capability()` returns `supports_multi_image=false`,
   `maximum_images=0`, and `capability_source=missing`. `LocalProvider` also
   defaults `supports_multi_frame=false`. This is why the formal run made zero
   model calls despite Doctor having verified E4B behavior.
2. `build_frame_windows()` emits every scene cluster as a mandatory window,
   including clusters with fewer than the configured minimum of 3 frames. Five
   such windows were emitted. `vision_pipeline` intentionally returns blocked
   before any provider call whenever any planned window is invalid, preserving
   the VID-15 fail-closed coverage contract.

The raw-response evidence index contains all 19 planned UUIDs, but all
`raw_response` and `provider_contract` values are empty. The 14 locally valid
entries are only frame-window validation passes, not model-result passes.

## Downstream gates deliberately not run

Because perception is the first authoritative failure, no Story generation,
human Apply, approval, formal Render, Render Report, Final QC, Delivery QA,
human final preview, final MP4, or `deliverable_ready=true` was attempted or
claimed. VID-11 must remain open.

## Integrity and evidence paths

- source media reverified at `2026-08-11T11:24:38Z`
- source SHA-256, size, and mtime: unchanged (`source-compare.json`)
- production data modified: `false`; only the isolated library was configured
- formal result: `library/05_index/perception_runs/f0ddb926-1992-4103-9ee0-d74f6b30b017/result.json`
- Doctor result: `doctor-full-gemma-e4b-v2.json`
- source integrity: `source-compare.json`

Working-tree disclosure: the user-requested Doctor per-request reasoning fix is
still local and uncommitted (`src/video_vault/doctor.py`,
`tests/test_doctor.py`). It is not part of baseline main. Its validation was
`40 passed` targeted and `656 passed, 2 skipped` full Python; `git diff --check`
passes apart from line-ending warnings. No branch, commit, push, or PR was made
for this acceptance run.