# Codex Master Plan - video-vault-ai

本文件是給 Codex 優先閱讀的長期改善計劃。每次實作前，請先閱讀：

1. `AGENTS.md`
2. `README.md`
3. `docs/codex_master_plan.md`

## 產品定位

`video-vault-ai` 是 Windows 本機 AI 剪片助手，不是全自動亂剪工具。

核心原則：

- WebUI-first：主要工作流從 `python -m video_vault ui` 操作。
- Project-first：一個 project 可以包含多支 clips。
- 先做 clip-level perception，再做 project-level story / plan。
- AI 先產生 plan / story，使用者 review 後才能正式 render。
- `needs_review` 必須是實際 gate，不只是 UI 狀態。
- 修改 plan / segment / BGM / 排序 / content_type / 調色 reference 後，狀態必須回到 `needs_review`。
- rendered 後再修改 plan，不能覆蓋舊版本，必須建立新版本。
- 不要自動覆蓋或破壞原始素材。
- BGM 必須保留來源與授權資訊。

目前優先影片類型：

- coffee diary montage
- matcha diary montage
- travel diary / vlog
- process montage

但架構要保留：

- tutorial
- recipe
- review
- shorts
- highlight

同一批素材未來應可產生多種 plan，例如日記版、教學版、短影音版。

## OpenMontage Reference Workflow Additions

參考 `calesthio/OpenMontage` 的流程治理概念，但不要直接複製其程式碼，也不要把本專案改成全自動雲端影片生成工作室。

本專案主軸仍然是：

```text
本機素材
→ clip-level perception
→ project-level story planning
→ WebUI human review
→ approved
→ HyperFrames / OpenCut / DaVinci / FFmpeg output
```

OpenMontage 值得借鏡的是 pipeline governance、quality gates、decision log、checkpoints、reference-driven planning，而不是整套 provider / stock footage / AI video generation 流程。

### Current Local Implementation Check

目前本地端已經有：

- WebUI 工作台。
- Project 建立與多素材匯入。
- clip-level 內容感知。
- frame extraction。
- mock / local / cloud analyzer。
- segment merge。
- `build_project_plan`。
- `project_script.md`。
- `review_status.json`。
- approve / reject。
- BGM 資料庫與授權紀錄。
- color preview。
- OpenCut handoff。
- HyperFrames 初剪。
- DaVinci export 雛形。
- 單支影片 `render-approved` approval gate。
- pytest 基礎測試。

目前本地端尚未完整實作：

- `pipeline_defs/` pipeline manifests。
- `skills/` stage director skill docs。
- project-level decision log。
- project-level checkpoints。
- pre-render validation。
- post-render self-review。
- reference-driven planning。
- style playbooks。
- render runtime locking。

### Addition A - Pipeline Manifest

目標：不要把 coffee / matcha / travel / tutorial 的剪輯流程全部寫死在 Python 裡。

新增建議：

```text
pipeline_defs/
├─ coffee_diary.yaml
├─ matcha_diary.yaml
├─ travel_diary.yaml
├─ tutorial.yaml
└─ shorts.yaml
```

每個 pipeline manifest 應定義：

```yaml
pipeline_id: coffee_diary
display_name: Coffee Diary
content_types:
  - diary_montage
  - coffee_process

stages:
  - ingest
  - perception
  - clip_summary
  - story_planning
  - review
  - approval_gate
  - pre_render_validation
  - render
  - post_render_review

requires_approval_before:
  - render
  - opencut_render_clips
  - hyperframes_mp4
  - davinci_create_timeline

allowed_before_approval:
  - story_outline
  - color_preview
  - html_preview
  - dry_run
```

第一版不需要把所有流程引擎化，但至少要能：

```text
content_type
→ 選 pipeline manifest
→ build_project_plan 讀到 pipeline metadata
→ project_plan.json 記錄 pipeline_id
```

### Addition B - Stage Skill Docs

目標：把剪輯規則寫成 Markdown skill，不要全部藏在 code 裡。

新增建議：

```text
skills/
├─ pipelines/
│  ├─ coffee_diary/
│  │  ├─ story_planning.md
│  │  ├─ pacing.md
│  │  └─ audio_policy.md
│  ├─ matcha_diary/
│  ├─ travel_diary/
│  └─ tutorial/
├─ review/
│  ├─ review_gate.md
│  └─ revision_notes_to_plan.md
└─ creative/
   ├─ pickup_shot_repositioning.md
   └─ diary_style.md
```

這些 Markdown 給 Codex / Hermes / local model 讀，用來明確定義：

- 咖啡日記怎麼剪。
- 抹茶刷茶聲怎麼保留。
- 旅行補拍外觀怎麼重排。
- 教學型影片怎麼保持步驟順序。
- review notes 怎麼轉成 timeline 修改。

### Addition C - Decision Log

目標：所有重要剪輯決策都要能追蹤。

新增：

```text
08_projects/project_x/decisions/decision_log.jsonl
```

每筆 decision 記錄：

```json
{
  "created_at": "2026-06-25T12:00:00",
  "project_id": 1,
  "plan_id": "diary_montage_v002",
  "decision_type": "segment_reposition",
  "decision": "move cafe exterior before first cafe interior segment",
  "reason": "外觀雖然最後補拍，但敘事上屬於剛到咖啡廳的 establishing shot",
  "source": "review_notes",
  "confidence": 0.84,
  "affected_segments": ["seg_014"]
}
```

需要記錄的決策：

- 為什麼選某些 segments。
- 為什麼排除某些 segments。
- 為什麼補拍外觀被移到前面。
- 為什麼選某個 HyperFrames template。
- 為什麼選某個 render runtime。
- 為什麼保留或降低原音。
- 為什麼使用某個 color profile。
- 為什麼 BGM 被選用或被拒絕。

### Addition D - Checkpoints

目標：長流程可恢復、可追蹤、可 debug。

新增：

```text
08_projects/project_x/checkpoints/
├─ perception_done.json
├─ plan_v001_created.json
├─ review_feedback_applied.json
├─ pre_render_validation_passed.json
└─ post_render_review_passed.json
```

每個 checkpoint 至少包含：

```json
{
  "checkpoint_id": "perception_done",
  "project_id": 1,
  "plan_id": "diary_montage_v001",
  "status": "passed",
  "created_at": "2026-06-25T12:00:00",
  "inputs": [],
  "outputs": [],
  "warnings": [],
  "errors": []
}
```

這要和 jobs persistence 串起來。UI 重開後仍應知道目前 project 跑到哪個 stage。

### Addition E - Pre-render Validation

目標：正式輸出前先擋掉錯誤狀態，不要 render 完才發現缺素材、沒授權、或 plan 沒審核。

新增 `pre_render_validation`，正式 render 前必須檢查：

- project status 是否 approved。
- latest plan 是否 approved。
- `review_status.approved_by_user == true`。
- 所有 source files 是否存在。
- 所有 segment start / end 是否合法。
- segment duration 是否大於 0。
- BGM 是否有 source / license / attribution。
- render runtime 是否已選定並鎖定。
- color profile 是否存在或明確設定為 skip。
- output path 不會覆蓋既有 rendered version。
- 若使用外部素材，必須有來源與授權資訊。

輸出：

```text
08_projects/project_x/validation/pre_render_report.json
08_projects/project_x/validation/pre_render_report.md
```

### Addition F - Post-render Self-review

目標：render 後自動做基本品質檢查。

新增 `post_render_review`，render 完後檢查：

- `ffprobe` 可讀。
- duration 是否符合 plan 預期。
- resolution / fps 是否符合 export profile。
- 抽樣 frames 是否有黑畫面。
- 音訊是否全靜音。
- 音訊是否 clipping。
- BGM 是否成功混入。
- 若有字幕 / title cards，抽樣 frame 應可看到。
- output file size 是否異常過小。

輸出：

```text
08_projects/project_x/validation/post_render_report.json
08_projects/project_x/validation/post_render_report.md
```

只有 post-render self-review 通過，project 才能標記 `rendered_success`。如果未通過，標記 `rendered_needs_review`，並回報問題給 WebUI。

### Addition G - Render Runtime Locking

目標：避免 plan 審核時說用 HyperFrames template，實際 render 卻偷偷改成 FFmpeg 或 DaVinci。

project plan 應新增：

```json
{
  "render_runtime": "hyperframes",
  "render_runtime_locked": true,
  "runtime_reason": "需要 template title cards 和 HTML/GSAP motion",
  "fallback_runtime": "ffmpeg",
  "fallback_requires_review": true
}
```

規則：

- render runtime 在 plan proposal / review 階段決定。
- approved 後不能靜默更換 runtime。
- 更換 runtime 必須建立新 plan version，並回到 `needs_review`。
- fallback render 可以做 dry-run，但正式輸出要重新審核。

### Addition H - Reference-driven Planning

目標：使用者可以指定參考影片或參考專案，讓系統學習節奏與風格。

新增：

```text
08_projects/project_x/reference/
├─ reference_project.json
├─ reference_pacing.json
└─ reference_style.md
```

可分析：

- 開場長度。
- 平均 shot duration。
- detail shots 比例。
- establishing shots 使用位置。
- BGM density。
- natural sound 保留比例。
- title card style。
- transition style。
- ending style。

規則：

- reference 只影響 plan proposal。
- 參考影片不應自動覆蓋使用者審核。
- 套用 reference 後仍必須 review。
- 如果 reference 與 content_type 衝突，應列入 warning。

### Addition I - Style Playbooks

目標：把影片風格獨立成可選 playbook，而不是只靠 prompt。

新增：

```text
styles/
├─ coffee_slow_life.yaml
├─ matcha_clean_minimal.yaml
├─ travel_diary_soft.yaml
├─ tutorial_clear_steps.yaml
└─ shorts_fast_hook.yaml
```

每個 playbook 定義：

```yaml
style_id: coffee_slow_life
display_name: Coffee Slow Life
pace: slow
title_cards: minimal
natural_sound: high
bgm_density: low
transition: clean_cut
color_tone: warm_soft
preferred_shots:
  - hands
  - detail
  - pour
  - finished_drink
avoid:
  - over_captioning
  - tutorial_voiceover
  - aggressive_zoom
```

project plan 應記錄：

```json
{
  "style_playbook": "coffee_slow_life",
  "style_reason": "使用者偏好個人日記感，不是教學或業配"
}
```

### Addition J - Resource Governance

目標：避免本地 VRAM、雲端 API、下載外部素材、BGM 授權失控。

應管理：

- local AI busy / queue。
- model context / prompt budget。
- cloud API 是否允許。
- render time estimate。
- BGM license risk。
- 是否允許自動下載外部素材。
- 是否允許使用 AI-generated image/video。

第一版預設：

```yaml
resources:
  allow_cloud_ai: false
  allow_external_stock: false
  allow_auto_download_bgm: false
  allow_ai_generated_video: false
  require_user_approval_for_external_assets: true
```

### Suggested Implementation Order

這些 OpenMontage-style 流程不要優先於基本安全流程。

建議順序：

1. Phase 0 approval gate。
2. Phase 1 frontend modernization。
3. Review notes feedback loop。
4. Plan versioning。
5. Decision log + checkpoints。
6. Pre-render validation + post-render self-review。
7. Pipeline manifest + skill docs skeleton。
8. Render runtime locking。
9. Reference-driven planning。
10. Style playbooks。
11. Resource governance。

第一個可交給 Codex 的小任務：

```text
請先不要接外部素材與雲端 provider。先新增本機 OpenMontage-style workflow skeleton：
1. 新增 docs/openmontage_reference_workflow.md。
2. 新增 pipeline_defs/coffee_diary.yaml、matcha_diary.yaml、travel_diary.yaml。
3. 新增 skills/review/review_gate.md、skills/review/revision_notes_to_plan.md、skills/creative/pickup_shot_repositioning.md。
4. 新增 decision log schema 文件 docs/decision_log.md。
5. 新增 checkpoint schema 文件 docs/checkpoints.md。
6. 不要改 render 行為，不要繞過 approval gate。
7. 跑 pytest，確認現有測試不壞。
```

## Local Model Policy

本專案的本地模型預設使用 LM Studio / OpenAI-compatible endpoint。

目前 `config.yaml` 實際設定為：

```yaml
ai:
  provider: "local"
  local:
    base_url: "http://127.0.0.1:1234/v1"
    model: "qwen/qwen2.5-vl-7b"
```

注意：`:1234/v1` 是正確方向。不要把預設改回 Ollama `:11434`，除非使用者明確要求支援 Ollama。

### Current Local Model Call Sites

目前本地模型只在內容感知階段被調用：

- 單支 clip 跑感知
- Project 跑待感知
- Project 全部重跑感知
- Inbox 匯入後自動分析
- CLI analyze / perceive 類流程

目前這些流程不應調用本地模型：

- review gate
- render
- color preview
- OpenCut / HyperFrames / DaVinci export
- plan versioning

### Future Local Model Call Sites

使用者已要求「審查時可以調整方向」，因此未來 review / revision loop 也可能調用模型，但必須受控。

未來可調用模型的 review/revision 場景：

- 將 review notes 轉成 structured edit instructions。
- 將「咖啡廳外觀移到剛到咖啡廳」轉成 story_position / insert rule。
- 將「咖啡段慢一點、保留刷茶聲」轉成 segment speed / audio policy。
- 將無法處理的備註列入 `feedback_unresolved`。
- 依 content_type 重新生成 story outline / timeline plan。

### Model Role Split

目前可支援兩種本地模型配置：

#### A. Two-stage model mode

`qwen/qwen2.5-vl-7b` 目前主要用途是 vision perception，也就是看抽出的 frame。它適合：

- frame image understanding
- coffee / matcha / travel visual tagging
- shot / scene / action recognition
- visual usefulness scoring

如果使用者本機另有 Gemma 4 文字模型，而且該版本沒有圖像識別能力，Gemma 4 不應取代 qwen2.5-vl-7b 來做 frame perception。文字模型可用於：

- review notes parsing
- revision planning
- story outline rewriting
- timeline plan reasoning
- content_type / preset selection
- unresolved feedback summary

這種模式的最佳設計是 two-stage：vision model 先把畫面轉成 structured perception，text model 再依 review notes 和 structured perception 重排故事。

#### B. Unified multimodal model mode

如果使用者安裝的是 Gemma 4 12B 普通版，且 LM Studio 端確認該模型支援 image input / multimodal chat，則可考慮用同一個 Gemma 4 12B 同時處理：

- frame perception
- review notes parsing
- revision planning
- story outline rewriting
- timeline plan reasoning

這樣可簡化流程，避免 vision model 和 text model 之間切換，也降低同時載入兩個模型造成 VRAM 不足的風險。

但要注意：

- 只有具備 vision input 的 Gemma 4 12B 才能做 frame perception。
- text-only 量化版 Gemma 不能做圖像感知。
- 即使用單一 multimodal 模型，仍必須走 local AI queue，避免 analyze / revision 並行請求造成 VRAM 或 context 壓力。
- 是否比 qwen2.5-vl-7b 更快，需要以實測為準；12B multimodal 可能文字規劃更好，但 frame perception 速度與 VRAM 壓力不一定低於 7B vision model。

建議 config 方向：

```yaml
ai:
  provider: "local"
  local:
    base_url: "http://127.0.0.1:1234/v1"
    model_mode: "unified_multimodal"  # or "two_stage"
    model: "gemma-4-12b-multimodal"
    vision_model: "gemma-4-12b-multimodal"
    revision_model: "gemma-4-12b-multimodal"
    max_concurrent_requests: 1
```

Two-stage config 範例：

```yaml
ai:
  provider: "local"
  local:
    base_url: "http://127.0.0.1:1234/v1"
    model_mode: "two_stage"
    vision_model: "qwen/qwen2.5-vl-7b"
    revision_model: "gemma-4-local-quant"
    max_concurrent_requests: 1
```

若 `revision_model` 為空，則 fallback 使用 `model` 或 `vision_model`。第一版可以先只支援同一個 endpoint，不要自動同時載入兩個模型。

### Context Window Policy

不要所有任務都固定開 64K。context window 越大，KV cache 越吃 VRAM，速度也可能下降。Hermes Agent 開 64K 可以理解，因為它需要讀程式碼與長任務上下文；但影片助手內部的模型呼叫應依任務分層。

建議：

```yaml
ai:
  local:
    context:
      frame_perception: 8192
      batch_frame_perception: 16384
      review_revision: 32768
      full_project_planning: 65536
      code_agent: 65536
```

任務建議：

1. 單張 frame perception：8K 通常足夠。
2. 一次少量 frames 的 batch perception：16K。
3. review notes → structured edit instructions：16K 到 32K。
4. project-level story / timeline planning：32K 起跳，大 project 再用 64K。
5. Codex / Hermes 這種讀 repo、改程式、長任務 agent：64K 合理。

重要規則：

- frame perception 不要一次塞整支影片所有 frames。
- 優先把每支 clip 壓成 structured perception summary，再交給 revision / planning。
- review/revision 不應讀所有 raw frame analysis，應讀 clips / segments / story groups / review notes 的摘要。
- 如果 context 超過設定，先做 hierarchical summarization：clip summary → group summary → project summary。
- 若 VRAM 緊張，預設把 review_revision 設 32K，不要一開始就 64K。
- 64K 只給 full_project_planning 或 code agent 類工作使用。

### VRAM / Concurrency Safety

本地模型可能吃大量 VRAM。尤其內容感知可能使用 vision model，review/revision 又可能同時呼叫模型，必須避免同時打開兩個模型或並行請求造成 VRAM 不足。

要求：

1. 本地 AI 呼叫必須走同一個 queue / worker。
2. 預設 `max_concurrent_local_ai_requests = 1`。
3. 同一時間只允許一個 local model job 執行。
4. 如果 analyze job 正在跑，review/revision model job 必須排隊或提示使用者稍後再試。
5. 如果 review/revision 正在跑，analyze job 必須排隊或提示使用者稍後再試。
6. 不要為 perception 和 revision 同時載入兩個不同本地模型，除非使用者明確設定並承擔 VRAM 風險。
7. 第一版建議 perception 與 revision 共用同一個 LM Studio endpoint / model。
8. WebUI 應顯示 local AI busy 狀態，例如「本地模型忙碌中，等待目前感知工作完成」。
9. jobs table 持久化後，local AI queue 也應寫入 jobs 狀態。

建議 config：

```yaml
ai:
  provider: "local"
  local:
    base_url: "http://127.0.0.1:1234/v1"
    model: "qwen/qwen2.5-vl-7b"
    max_concurrent_requests: 1
    use_same_model_for_revision: true
    revision_model: ""
```

如果未來要分開 vision model / text planner model，必須仍然遵守 local queue，不能同時呼叫兩個本地模型。

---

## Current State Snapshot

目前已存在：

- WebUI 工作台
- Project 建立與多素材匯入
- clip-level 內容感知
- mock / local / cloud analyzer
- frame extraction
- segment merge
- `build_project_plan`
- `project_script.md`
- `review_status.json`
- approve / reject
- BGM 資料庫與授權紀錄
- color preview
- OpenCut handoff
- HyperFrames 初剪
- DaVinci export 雛形
- 單支影片 `render-approved` approval gate
- pytest 基礎測試

目前主要缺口：

- project-level approval gate 尚未完整落實。
- HyperFrames / OpenCut / DaVinci 正式輸出未全面受 `approved` gate 控制。
- review notes 目前不會影響下一版 plan。
- WebUI 審核備註表單可能沒有正確送出到 approve / reject action。
- `project_plan.json` 每次 build 會覆蓋，缺少 versioning。
- rendered 後修改 plan 沒有建立新版本。
- 缺少 segment-level review UI。
- perception schema 太簡單，只有 summary / tags / score。
- planner 太依賴 time_of_day / clip order，缺少 story_position。
- 補拍鏡頭，例如最後補拍咖啡廳外觀，無法自動移到剛到咖啡廳段落。
- content_type 有欄位，但 planner 尚未真正分流。
- project-level color matching 尚未完成。
- BGM auto download 應改成使用者確認。
- 上傳同名素材可能覆蓋。
- `rename_after_perception` 會 rename 檔案，對原始素材不夠安全。
- Jobs 只存在 memory dict，重開 UI 後工作狀態消失。
- 目前 WebUI 是 Python `ui.py` 直接拼 HTML，前端互動與可維護性不足。
- 使用者希望導入 HyperFrames / Build Web Apps 類似的方式，改善 WebUI 的前端體驗。

---

## Phase 0 - Flow Safety / Approval Gate

優先實作。不要先改 AI prompt。

目標：避免未審核就正式輸出，讓 `needs_review` 有實際作用。

### Requirements

1. 新增 project-level approval gate。
2. `needs_review` / `rejected` / `draft` 狀態禁止正式 render。
3. HyperFrames render、OpenCut `render_clips`、DaVinci create timeline 都必須檢查 approved。
4. 允許 `needs_review` 階段做 preview / dry-run / story outline / color preview。
5. 任何會改變剪輯結果的操作都要把狀態設回 `needs_review`。
6. 補測試，確認未 approved 時不能輸出正式 MP4 或 graded clips。
7. 保持 WebUI-first。
8. 不要自動覆蓋原始素材。

### Approval Rules

未 approved 前允許：

- story outline
- project_script.md
- dry-run
- color preview
- preview grid
- OpenCut handoff JSON / CSV / README

未 approved 前禁止：

- HyperFrames MP4 render
- OpenCut `render_clips` / graded clips
- DaVinci create timeline
- final export
- single-video `render-approved`

### Suggested API

新增於 `project.py` 或新的 `review_gate.py`：

```python
def can_project_render(cfg: dict, db: Path, project_id: int) -> tuple[bool, str]:
    ...


def assert_project_approved(cfg: dict, db: Path, project_id: int, action: str = "render") -> None:
    ...
```

檢查：

- project exists
- project.status == `approved`
- `review_status.json` exists
- `review_status.approved_by_user == true`
- latest plan status == `approved`

### Files likely involved

- `src/video_vault/project.py`
- `src/video_vault/ui.py`
- `src/video_vault/hyperframes.py`
- `src/video_vault/opencut.py`
- `src/video_vault/cli.py`
- `src/video_vault/davinci/*`
- `tests/`

### Completion Criteria

- `pytest` passes.
- needs_review 時不能產生正式 MP4。
- needs_review 時不能產生 OpenCut graded clips。
- needs_review 時不能直接 create DaVinci timeline。
- approved 後才可以正式輸出。
- color preview / story outline / dry-run 仍可在 needs_review 執行。

---

## Phase 1 - Frontend Modernization / Build Web Apps Integration

目標：改善目前 WebUI 的可用性。現在 `ui.py` 直接用 Python string 拼 HTML，短期能跑，但不適合後續 segment review、縮圖預覽、拖拉排序、多版本比較、template 選擇與長任務狀態管理。

這個 phase 的方向是導入「Build Web Apps」概念：用更像現代 web app 的架構來重做前端，但後端仍保留 Python local-first workflow。

### Current State

目前 WebUI：

- Python `BaseHTTPRequestHandler`。
- HTML / CSS / form 都寫在 `ui.py`。
- 沒有前端 router。
- 沒有 component 化。
- 沒有互動式 table。
- 沒有 drag-and-drop segment ordering。
- 沒有即時 preview 狀態。
- 沒有好用的 review notes workflow。

目前功能能跑，但使用者覺得不好用。

### Direction

第一版不要直接推翻全部後端。建議採用漸進式改造：

```text
Python backend API + modern frontend app
```

建議前端位置：

```text
web/
├─ package.json
├─ index.html
├─ src/
│  ├─ main.tsx
│  ├─ App.tsx
│  ├─ api.ts
│  ├─ components/
│  ├─ pages/
│  └─ styles.css
└─ README.md
```

Python 端保留：

- `/api/projects`
- `/api/project`
- `/api/videos`
- `/api/bgm`
- `/api/project/analyze`
- `/api/project/build-plan`
- `/api/project/approve`
- `/api/project/reject`
- `/api/project/color-preview`
- `/api/project/opencut-export`
- `/api/project/hyperframes-export`

新增 static serving：

- dev mode：前端跑 Vite dev server。
- local packaged mode：Python server 可 serve `web/dist`。

### Recommended Stack

優先選：

- React + Vite + TypeScript
- CSS modules 或單純 CSS
- 不要一開始導入過重 UI framework

可接受：

- Tailwind CSS
- shadcn/ui style components
- TanStack Table，用於 segment review table

第一版不要上雲端，不要引入登入，不要做多人協作。

### Core UX Screens

1. Project Dashboard

- 專案列表
- 狀態 pill：draft / needs_review / approved / rendered
- 最近 job 狀態
- 新增 project
- 匯入 clips

2. Project Detail

- Project summary
- Clips list
- Analyze button
- Build plan button
- Review gate 狀態
- BGM 區塊
- Color preview 區塊
- HyperFrames / OpenCut export 區塊

3. Segment Review

- segments table
- thumbnail
- start / end
- include / exclude
- scene_role
- story_position
- audio_role
- speed
- notes
- drag reorder
- save review changes

4. Story Review

- project_script.md rendered preview
- revision notes textarea
- approve / reject / revise buttons
- feedback applied / unresolved display

5. Render / Export Panel

- 顯示 approval gate
- 未 approved 時正式輸出按鈕 disabled
- needs_review 時只允許 preview / dry-run

6. HyperFrames Template Panel

- template selector
- selected template preview
- template_id 寫入 plan / timeline
- HTML preview 可在 needs_review 產生
- MP4 render 必須 approved

### Build Web Apps / HyperFrames Use

HyperFrames 網站有 template / frame.md / video creation 的方向，因此本專案可以把它用於兩個地方：

1. 前端設計參考
   - 用 Build Web Apps 概念快速產生 WebUI 原型。
   - Codex 可根據現有 API 建立 React/Vite WebUI。
   - 產出的前端要落地到 repo，不要只是外部 prototype。

2. 影片模板引擎
   - HyperFrames templates 用於影片輸出視覺模板。
   - 前端 WebUI 要能選 template。

### API Contract First

前端重做前，先整理 API contract：

```text
docs/api_contract.md
```

至少定義：

- GET `/api/projects`
- GET `/api/project?id=...`
- GET `/api/videos`
- GET `/api/bgm`
- POST `/api/project/analyze`
- POST `/api/project/build-plan`
- POST `/api/project/approve`
- POST `/api/project/reject`
- POST `/api/project/review-segments`
- POST `/api/project/hyperframes-export`
- POST `/api/project/opencut-export`
```

### Compatibility Requirement

不要一次刪掉舊 WebUI。

第一版做法：

- 舊 `ui.py` 保留 server / API。
- 新前端放在 `web/`。
- 開發時可用 Vite。
- build 後輸出 `web/dist`。
- Python server 可 serve `web/dist`。
- 如果 `web/dist` 不存在，fallback 到舊 HTML UI。

### Completion Criteria

- 有 `web/` 前端專案。
- 可從 API 讀 projects / project detail。
- 可顯示 project dashboard。
- 可顯示 project detail。
- 可送出 approve / reject notes。
- 未 approved 時 render buttons disabled。
- 現有 Python API 與 pytest 不壞。
- README 或 docs 說明如何啟動前端。

### Suggested First Frontend Task

```text
請建立 web/ React + Vite + TypeScript 前端骨架，但不要移除既有 Python UI。

需求：
1. 新增 web/package.json、Vite、React、TypeScript。
2. 建立 API client。
3. 建立 Project Dashboard。
4. 建立 Project Detail 基礎頁。
5. 顯示 project status、clips、script、BGM、jobs。
6. approve / reject notes 要能送到後端。
7. render/export buttons 根據 approval gate disabled。
8. Python server 若偵測到 web/dist，serve 新前端；否則 fallback 舊 UI。
9. 補 docs，說明 dev 與 build 啟動方式。
10. 不要改 AI prompt。
```

---

## Phase 2 - Review Notes Feedback Loop

目標：使用者審核備註要能影響下一版 plan。

### Problem

目前 review notes 主要只是紀錄，不會被 `build_project_plan()` 使用。WebUI 的 notes textarea 也可能沒有正確跟 approve / reject action 綁在一起。

### Requirements

1. 修正 WebUI notes form，讓 approve / reject / revise 都能送出 notes。
2. 儲存 revision notes 到：
   - `review_status.json`
   - `feedback/revision_notes.md`
   - `feedback/revision_YYYYMMDD_HHMMSS.md`
3. `build_project_plan()` 讀取最新 feedback。
4. project plan 新增：
   - `feedback_applied`
   - `feedback_unresolved`
   - `revision_notes`
5. `project_script.md` 顯示：
   - 已套用的審核備註
   - 尚未能自動處理的備註
6. 新增 WebUI action：「依備註重建故事」。
7. 套用 feedback 後，狀態回 `needs_review`。

### Important Distinction

不是所有問題都要重跑感知。

例如：

- 「咖啡廳外觀移到剛到咖啡廳那邊」是 plan revision。
- 「這不是街景，是咖啡廳外觀」才是 perception correction。

### Completion Criteria

輸入備註：

> 最後補拍的咖啡廳外觀應該移到剛到咖啡廳那邊。

重建 plan 後，系統至少要：

- 將 feedback 寫入新 plan。
- 嘗試轉成 story_position / insert_reason。
- 若無法自動處理，列入 `feedback_unresolved`。
- project_script.md 顯示此備註的處理狀態。

---

## Phase 2 - Plan Versioning

目標：所有 plan 可追蹤、可回復、不覆蓋 rendered / approved 成果。

### Current Problem

目前 `build_project_plan()` 會覆蓋：

- `project_plan.json`
- `project_script.md`

### Target Structure

```text
08_projects/project_x/
├─ plans/
│  ├─ diary_montage_v001.json
│  ├─ diary_montage_v001.md
│  ├─ diary_montage_v002.json
│  ├─ diary_montage_v002.md
│  └─ latest.json
├─ project_plan.json
├─ project_script.md
├─ review_status.json
└─ feedback/
```

`project_plan.json` 和 `project_script.md` 可以保留為 latest copy，但不可作為唯一歷史。

### Version Rules

- 第一次 build：v001。
- needs_review 修改後重建：v002。
- approved 後如果修改：v002，並回 needs_review。
- rendered 後如果修改：一定建立新版本，不可覆蓋 rendered version。

### Plan Metadata

```json
{
  "project_id": 1,
  "plan_id": "diary_montage_v002",
  "version": 2,
  "content_type": "diary_montage",
  "status": "needs_review",
  "parent_plan_id": "diary_montage_v001",
  "created_at": "...",
  "created_reason": "revision_notes"
}
```

### Completion Criteria

- rendered plan 不會被覆蓋。
- 修改後產生 v002。
- latest 指向最新 plan。
- approved plan 一旦被修改，approved_by_user 失效並回 needs_review。

---

## Phase 3 - Segment Review Table

目標：WebUI 可逐段審核與修正剪輯決策。

### Segment Review Fields

每段顯示：

- clip_id
- filename
- thumbnail
- start_seconds
- end_seconds
- duration
- group
- score
- AI reason
- suggested_use
- scene_role
- story_position
- include / exclude
- audio policy
- speed
- notes

### Editable Operations

- 保留 / 不使用
- 調整 start / end
- 調整排序
- 移到某個 group
- 標記用途：opening / main / detail / transition / ending
- 標記為 establishing shot
- 標記為 pickup shot
- 保留原音
- 加速 / 放慢
- 加備註

### Segment Schema Example

```json
{
  "segment_id": "seg_001",
  "clip_id": "clip_003",
  "video_id": 3,
  "source_file": "...",
  "start_seconds": 10.5,
  "end_seconds": 16.2,
  "include": true,
  "manual_order": 4,
  "scene_role": "establishing_shot",
  "story_position": "cafe_arrival",
  "narrative_function": "location_intro",
  "audio_role": "keep_natural_sound",
  "speed": 1.0,
  "user_notes": "咖啡廳外觀，應該放在進店前"
}
```

每次修改 segment review：

- project status → `needs_review`
- plan status → `needs_review`
- approved_by_user → false

---

## Phase 4 - Pickup Shot / Story Position

目標：解決補拍鏡頭放錯敘事位置。

### Real User Case

最後補拍咖啡廳外觀，但剪輯上應該放到剛到咖啡廳那邊。

### Key Concept

拍攝時間不等於敘事時間。

需要區分：

- `capture_time`：實際拍攝時間
- `clip_order`：素材順序
- `story_position`：故事位置

### New Fields

```json
{
  "scene_role": "establishing_shot",
  "narrative_function": "location_intro",
  "story_anchor": "cafe_arrival",
  "can_move_across_time": true,
  "is_pickup_shot": true,
  "insert_strategy": "place_before_related_interior_scene",
  "insert_reason": "外觀雖然最後拍，但敘事上應放在剛到咖啡廳"
}
```

### Planner Rules

如果 segment 是：

- 店面外觀
- 招牌
- 門口
- 建築外觀
- 景點入口
- 環境 establishing shot

即使拍攝時間較晚，也應插入相關場景第一次出現前。

Examples：

- 咖啡廳外觀 → 第一個 cafe interior / coffee segment 之前
- 餐廳招牌 → 第一個 food segment 之前
- 景點入口 → 景點主畫面之前
- 飯店外觀 → lobby / room 畫面之前

---

## Phase 5 - Perception Schema v2

目標：讓 AI 不只知道畫面是什麼，也知道剪輯用途。

### Current Schema

目前主要是：

- summary
- tags
- visual_quality_score
- usefulness_score
- suggested_use

### Target Schema

```json
{
  "schema_version": "2.0",
  "prompt_version": "perception_v2",
  "summary": "",
  "tags": [],
  "shot_type": "wide | medium | closeup | detail | hands | pov",
  "scene_role": "opening | establishing_shot | main_action | detail | transition | ending | unusable",
  "motion_type": "static | handheld | walking | hands_action | pour | whisk | pan | tilt",
  "stability": "stable | slight_shake | shaky",
  "visual_quality_score": 0.0,
  "usefulness_score": 0.0,
  "diary_value": 0.0,
  "instruction_value": 0.0,
  "commercial_value": 0.0,
  "natural_sound_priority": 0.0,
  "process_step": "",
  "story_anchor": "",
  "suggested_use": "",
  "caption_hint": "",
  "audio_hint": ""
}
```

### Coffee Process Steps

- beans
- grinding
- filter_rinse
- dose
- bloom
- first_pour
- second_pour
- dripping
- finished_drink
- taste
- cleanup

### Matcha Process Steps

- bowl
- chasen
- powder
- sifting
- water
- whisking
- foam
- finished_drink
- taste
- cleanup

### Travel Story Anchors

- departure
- arrival
- street
- transport
- cafe_exterior
- cafe_interior
- ordering
- food
- drink
- walking
- landscape
- night
- leaving

### Cache Rule

Cache key 應包含：

- video content hash
- timestamp
- provider
- model
- prompt_version
- schema_version

避免 schema 升級後誤用舊 cache。

---

## Phase 6 - Planner Presets

目標：同素材可產生不同影片版本。

### Target Planners

- diary_montage
- travel_diary
- coffee_process
- matcha_process
- tutorial
- recipe
- shorts
- highlight
- review

可先實作：

- diary_montage
- travel_diary
- process_montage

保留 tutorial schema，不一定第一版完整實作。

### Diary Montage Rules

- 安靜
- 少字幕
- 保留氛圍聲
- 以 mood / detail / hands / finished shot 為主
- 避免太像教學或業配

### Coffee Process Rules

```text
opening atmosphere
→ beans / tools
→ grinding
→ rinse / setup
→ bloom
→ pour
→ dripping
→ finished cup
→ quiet ending
```

### Matcha Process Rules

```text
tools
→ powder
→ sifting
→ water
→ whisking
→ foam payoff
→ finished drink
→ quiet ending
```

### Travel Diary Rules

```text
departure / moving
→ arrival
→ establishing shot
→ main activity
→ detail
→ food / cafe
→ walking / transition
→ ending
```

---

## Phase 7 - Project-level Color Matching

目標：不只是套 LUT，而是整支影片色調一致。

### Current State

`color.py` 有：

- dji_lut
- safe_restore
- warm_food
- brightness stats
- ffmpeg eq

但目前是單支影片修正，不是 project-level matching。

### Target Structure

```text
08_projects/project_x/color/
├─ project_color_profile.json
├─ reference_clip.json
├─ clip_001_color.json
├─ clip_002_color.json
└─ preview_grid.jpg
```

### Workflow

1. 每支 clip 抽樣 frame。
2. 計算亮度 / highlight / saturation / rough color temperature。
3. 選 reference clip。
4. 其他 clip 往 reference 靠。
5. 套 DJI LUT。
6. 再做微調。
7. 輸出 preview grid。

### UI

- 選某段作為調色 reference。
- 產生 preview grid。
- 套用 project color profile。

### Gate

- color preview 可在 `needs_review` 執行。
- 正式 graded clips 必須 approved 後才能 render。

---

## Phase 8 - HyperFrames Template Integration

目標：不要只把 HyperFrames 當成片段串接工具，要把它當成 template-based visual engine。

HyperFrames 網站有 template / frame.md / URL-to-video 概念，因此本專案應支援「依 content_type 選模板」，讓 diary、travel、coffee、matcha、tutorial、shorts 可有不同視覺節奏與版型。

### Current State

目前 `hyperframes.py` 會輸出：

- `timeline.json`
- `index.html`
- `README.md`
- `story_draft_fast.mp4`

但目前的 HTML timeline 是硬寫在 `_html()` 裡，還沒有 template registry，也沒有依 content_type / plan preset 切換模板。

### Requirements

1. 新增 HyperFrames template registry。
2. 支援依 project `content_type` 選擇模板。
3. 支援 template metadata，例如：
   - template_id
   - display_name
   - content_type
   - aspect_ratio
   - title_card_style
   - transition_style
   - default_clip_duration
   - bgm_policy
   - natural_sound_policy
4. 將目前硬寫的 `_html()` 拆成可替換 template。
5. 保留 simple built-in template，確保現有測試仍能過。
6. WebUI 可顯示目前選用 template。
7. 後續可匯入 HyperFrames 官方或社群模板，但第一版先做本地 template abstraction。
8. template 只能影響 visual / timing presentation，不可繞過 review gate。
9. 正式 MP4 render 仍必須 approved；HTML preview 可在 `needs_review` 產生。

### Suggested Template Structure

```text
src/video_vault/templates/hyperframes/
├─ simple_story/
│  ├─ template.json
│  └─ index.html
├─ coffee_diary/
│  ├─ template.json
│  └─ index.html
├─ matcha_diary/
│  ├─ template.json
│  └─ index.html
└─ travel_diary/
   ├─ template.json
   └─ index.html
```

### Suggested Template Metadata

```json
{
  "template_id": "coffee_diary",
  "display_name": "Coffee Diary",
  "content_types": ["diary_montage", "coffee_process"],
  "aspect_ratio": "16:9",
  "title_card_style": "minimal_lower_left",
  "transition_style": "clean_cut",
  "default_clip_duration": 2.8,
  "detail_clip_duration": 1.4,
  "main_action_speed": 1.0,
  "waiting_speed": 3.0,
  "bgm_volume_db": -24,
  "natural_sound_policy": "prefer_hands_pour_drip"
}
```

### First Built-in Templates

1. `simple_story`
   - 保留現有行為。
   - 用於測試與 fallback。

2. `coffee_diary`
   - 慢節奏。
   - 特寫與手部優先。
   - 字卡少。
   - 保留倒水 / 滴落 / 杯聲。

3. `matcha_diary`
   - 乾淨、留白。
   - 刷茶聲優先。
   - foam payoff 不要剪太短。

4. `travel_diary`
   - 依 story group 顯示地點 / 時段字卡。
   - establishing shot 放在場景前。
   - 街景 / 移動作轉場。

### Completion Criteria

- HyperFrames export 可選 template。
- 不指定 template 時使用 simple_story，現有測試不壞。
- project plan / timeline.json 記錄 `template_id`。
- WebUI 顯示目前 template。
- `needs_review` 可產生 HTML preview。
- 未 approved 不可輸出 MP4。

---

## Phase 9 - BGM / Natural Sound Policy

目標：日記感影片保留自然聲，BGM 不搶戲。

### Requirements

1. auto download BGM 改為 confirm required。
2. segment 加入 audio_role。
3. coffee / matcha 自然聲優先。
4. BGM ducking metadata。
5. HyperFrames / ffmpeg render 使用 audio policy。

### Audio Schema

```json
{
  "audio_role": "mute | keep_natural_sound | lower_original | voice_focus",
  "natural_sound_priority": 0.8,
  "bgm_ducking": true,
  "bgm_volume_db": -24
}
```

Coffee / matcha 優先保留：

- 倒水聲
- 滴落聲
- 刷茶聲
- 杯碗聲
- 磨豆聲
- 包裝聲

---

## Phase 9 - Asset Safety

目標：避免覆蓋與誤改原始素材。

### Problems

1. `upload_project()` 同名檔可能覆蓋。
2. `rename_after_perception()` 會直接 rename 檔案。

### Requirements

1. upload_project 同名檔不覆蓋。
2. project source 使用 `clip_001_` prefix 或 hash。
3. `rename_after_perception()` 不直接改原始素材。
4. 增加 display_name / detected_name。
5. 補測試。

### Completion Criteria

- 上傳兩支同名影片不會互相覆蓋。
- 原始檔案不被 rename。

---

## Phase 10 - Jobs Persistence

目標：長任務可追蹤、可恢復。

### Current State

WebUI `JOBS` 是 memory dict，重開 UI 會消失。

### Target Tables

```sql
jobs
job_steps
```

Fields：

- id
- project_id
- job_type
- status
- progress
- message
- started_at
- updated_at
- finished_at
- error_message
- output_path

Used for：

- analyze
- color_preview
- opencut
- hyperframes
- render

### Completion Criteria

重開 UI 後仍可看到上一個 job 狀態。

---

## Phase 11 - Documentation Update

目前 `docs/workflow.md` 還是舊流程。

需要更新：

- `README.md`
- `docs/workflow.md`
- `docs/roadmap.md`
- `docs/schema.md`
- `config.example.yaml`

新增文件：

- `docs/review_gate.md`
- `docs/plan_versioning.md`
- `docs/perception_schema_v2.md`
- `docs/planner_presets.md`
- `docs/color_workflow.md`

---

## Suggested First Task for Codex

請優先實作 Phase 0，不要一次做全部。

Task：

```text
請只做流程安全改善，不要改 AI prompt。

目標：
1. 新增 project-level approval gate。
2. needs_review / rejected / draft 狀態禁止正式 render。
3. HyperFrames render、OpenCut render_clips、DaVinci create timeline 都必須檢查 approved。
4. 允許 needs_review 做 preview / dry-run / story outline / color preview。
5. 任何會改變剪輯結果的操作都要把狀態設回 needs_review。
6. 補測試，確認未 approved 時不能輸出正式 MP4 或 graded_clips。
7. 保持 WebUI-first。
8. 不要自動覆蓋原始素材。
```

完成後請執行：

```powershell
pytest
```

回報：

- 修改了哪些檔案
- 哪些 action 被 gate 擋住
- 哪些 preview 仍可在 needs_review 執行
- 新增了哪些測試
