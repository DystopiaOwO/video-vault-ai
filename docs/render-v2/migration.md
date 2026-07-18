# Render Pipeline v2 相容性遷移

## Manifest 優先

HyperFrames 與 OpenCut handoff 會先讀取專案中的 Render Manifest：

- `08_projects/project_<id>/render_manifest.json`
- `08_projects/project_<id>/render/render_manifest.json`
- `08_projects/project_<id>/output/render/render_manifest.json`

有 Manifest 時，片段順序使用人工審核後的 `manual_order`，`include=false` 的片段不會輸出；來源剪點使用 `source_in_ms`／`source_out_ms`，播放長度為：

```text
(source_out - source_in) / speed
```

HyperFrames HTML 會以 `data-media-start` 保留來源起點，Manifest 的 `overlays` 會轉成預覽字卡。OpenCut JSON、CSV 與 README 會保留 `order`、`start_seconds`、`end_seconds`、`speed`、`audio_role` 等人工審核欄位。

沒有 Manifest 時，OpenCut 才會回退到既有 `project_plan.json` segments；這條路徑保留舊專案相容性，但不會取代 Manifest 規格。

## Approval Gate

產生 JSON／CSV／README handoff 不需要核准，方便在 `needs_review` 階段檢查剪輯計畫。OpenCut `render_clips=true` 與 HyperFrames MP4 輸出屬於會改變素材或輸出的操作，必須同時符合 project、review、plan 的核准條件。

若未核准，系統會回報 gate reason，不建立新的 `graded_clips`。

## Legacy Rough Preview

`render_fast_draft()` 保留給舊 CLI 與粗略確認順序使用，回傳 `kind=legacy_rough_preview`。它不是 Accurate Preview，也不是 Final Render；正式輸出應使用 Render v2 API／Render Engine。

## 舊 Renderer

`renderer.render_approved()` 保留既有 CLI 入口。當整合層提供 `render_approved_project`、`render_final` 或 `render_approved_legacy` adapter 時會優先委派；沒有 adapter 才使用相容 fallback。fallback 的片段與最終 concat 會解碼／重新編碼，不使用純 `-c copy` 當作 Accurate／Final 輸出。

## 已知限制

- 本相容層不修改 DaVinci Resolve 流程。
- 本相容層不建立第二套 Manifest、Timeline 或 Job 規格。
- OpenCut graded clip 的色彩處理仍由既有 color pipeline 決定；完整音訊混音由 Render v2 Assembly 負責。
- 真正的 Final Render 是否可執行，仍須通過 Render v2 Preflight、Approval Hash 與 QC。

