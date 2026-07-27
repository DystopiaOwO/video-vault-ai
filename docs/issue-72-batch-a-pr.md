# Issue #72 Batch A

本 Draft PR 只實作 Issue #72 的 Phase 1～3，不包含 Batch B；沿用既有 perception runs、project media stable identity、storyboard、approval snapshot 與 BGM 架構，沒有建立平行流程。

## Baseline

逐條 completed / partial / missing 盤點與採用方案見 [issue-72-batch-a-inventory.md](issue-72-batch-a-inventory.md)。與現有程式衝突時採用 Issue #72 的產品決策：授權證據不足的 BGM 不視為安全，正式核准預設 fail closed。

## Checkpoints

### Checkpoint 1：Ownership、Perception、Stable Identity

Commit `777d23b`：

- 專案素材關係補上 source fingerprint、ownership state、migration generation。
- 新增可重複、可回滾的 project media ownership migration report。
- perception run 補上 provider/extractor contract、input snapshot、published revision、interrupted timestamp。
- 新增 `analysis_run_frames` 與 `analysis_run_segments`，感知結果先進 run scope，再發布到 live frames/segments。
- 保留 stable segment UUID、舊 storyboard/timing/audio/color/review state migration 與 restart recovery。

### Checkpoint 2：Target Duration、Visual Timeline

Commit `88079f3`：

- 新增 `duration-budget-v1`，依 target duration、group coverage、locked decision 與 deterministic ordering 產生 include/omitted/conflict/budget 結果。
- project plan 暴露 target、estimated、tolerance 與 group coverage。
- 新增 versioned `visual-timeline-v1` / `visual_items` chapter card contract，納入 project plan、Render Manifest、manifest hash 與 approval snapshot。
- 正式 overlay composition 與 runtime asset rendering保留給 Batch B。

### Checkpoint 3：BGM License、整合驗證

Commit `bfb9276`：

- `bgm_tracks`、API、project projection、manifest、approval snapshot 與 WebUI 補上 `attribution_status`、`license_status`、`license_verified_at`、`license_source_url`、`verification_source`、`verification_provenance`。
- legacy migration：有可靠 CC0/public-domain/self-owned evidence 才標 `not_required/verified`；有可靠 CC BY evidence 才標 `required/verified`；legacy 0 無證據標 `unknown/unverified`。
- manifest 輸出 `bgm_credits` 與 `unresolved_bgm_licenses`。
- unknown 或未 verified BGM 預設阻擋 approval；只接受一次性且明確列出 track IDs 的 acknowledgement。
- BGM library badge/filter 不再把 unknown 顯示成不需署名。

修復 commit `b2ee998`：來源檔在 perception run 建立前消失時，先持久化 failed run、錯誤與 project pointer，再保留原本的 `FileNotFoundError`，避免 Phase 1 contract 破壞既有失敗追蹤。

## 測試

- Checkpoint 3 相關回歸：`90 passed`。
- 完整 Python：`396 passed, 2 skipped`，本機 pytest runner 為 Python 3.10.11；CI 另以 Python 3.11/3.13 驗證。
- Encoding：`PYTHONPATH=src py -3.10 scripts/check_encoding.py` 通過。
- Diff：`git diff --check origin/main...HEAD` 通過。
- Frontend：`npm test` 為 `30 files / 223 tests passed`；`npm run build` 通過。
- FFmpeg E2E：`tests/test_media_smoke.py tests/test_segment_renderer_e2e.py tests/test_project_renderer_e2e.py tests/test_render_job_e2e.py` 為 `12 passed`。
- FFmpeg：8.1.2；ffprobe：8.1.2。

## CI

最新 PR CI Run：`30266783392`，全部成功：

- Windows authoritative / Python 3.11：success
- Ubuntu portability / Python 3.13：success
- Media Smoke / Ubuntu：success
- Frontend：success
- Select PR test layers：success

## Known Limitations

- Batch B 的正式 visual overlay composition、字型/資產 runtime rendering 與完整輸出階段不在本 PR。
- BGM 專用授權證據編輯畫面仍可在後續 UI 工作補強；本批已提供 server-side upload 欄位、狀態顯示與 approval fail-closed contract。
- 本 PR 沒有接外部素材、雲端 provider 或新的 perception/timeline/approval 平行系統。

## Final adversarial review

由一個唯讀 `gpt-5.6-luna` high-reasoning 子代理完成；子代理未修改檔案、未建立 commit/branch/PR，也未執行完整測試。審查範圍限定 PR #73 相對 `main` 的 diff 與直接呼叫鏈。

### Confirmed findings and fixes

- `38565e3`：正式 project perception 遇到同一 `video_id` 被多個專案共用時 fail closed，避免感知、命名與 migration 污染其他專案；migration 正式流程也固定只處理目標 project。
- `38565e3`：perception rollback snapshot 補上 project rows，失敗/取消時還原 project status、revision 與 workflow state。
- `38565e3`：metadata rollback snapshot 補上 storyboard、segment review、audio、color 等 user-authored state。
- `38565e3`：project plan 重新建立時先合併目前人工 trim/speed/include/lock，再計算有效 duration budget。
- `38565e3`：`license_status=invalid` 永久 fail closed，不受 acknowledgement 繞過；visual runtime assets 納入 approval asset fingerprint，缺失時拒絕核准。

### False positive / out of scope

- visual overlay 的正式合成、overlay duration 是否佔用 timeline 與 final parity 屬 Batch B；本 PR 只保留 versioned visual contract、manifest/hash/approval 與 runtime asset fail-closed，不宣稱已有正式 overlay render。
- legacy 直接呼叫 `migrate_segment_state_for_video(video_id)` 仍保留相容行為；正式 project perception 已不走跨 project migration，並對 shared legacy ownership fail closed。

### Final tests

- Python full suite：`401 passed, 2 skipped`；skip 為 Windows 不執行 POSIX process-group semantics。
- Frontend：`30 files / 223 tests passed`；production build passed。
- FFmpeg E2E：`12 passed`。
- Encoding check、`py -3.10 -m compileall -q src` 與 `git diff --check origin/main...HEAD` passed。
- 未重跑 Windows 真實專案 smoke；本輪未修改 FFmpeg runtime 或 Windows path 行為，CI Windows authoritative job 仍負責平台驗證。
