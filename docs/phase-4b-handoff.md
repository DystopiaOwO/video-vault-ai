# video-vault-ai Render Pipeline v2：Phase 4B 交接

> 交接日期：2026-07-19（Asia/Taipei）
> 明日從本檔案接續工作。

## 目前狀態

- Repository：`DystopiaOwO/video-vault-ai`
- Phase 4A PR #5 已合併至 `main`。
- Phase 4B 分支：`codex/render-v2-render-jobs`
- Phase 4B PR #6：<https://github.com/DystopiaOwO/video-vault-ai/pull/6>
- PR #6 目前保持開啟，尚未合併。
- PR #6 最新 commit：`0ff034528e2429c4d71ff2179915f52c27ddf83e`

## Phase 4B 已完成

已完成持久化 Render Job、FFmpeg 進度與精準取消：

- Job model、JSON store、狀態生命週期與重啟恢復。
- FIFO queue，預設同時只執行一個 render job。
- 同專案已有 active job 時避免重複建立。
- 啟動時將殘留的 queued/running/cancelling job 標記為 interrupted。
- FFmpeg `-progress pipe:1 -nostats` 解析真實進度。
- 保存 encoder、stage、segment、output、error、PID 與 log 資訊。
- Windows 只終止指定 Job 的 process tree；未使用全域 kill ffmpeg。
- 取消狀態：`cancelling` → `cancelled`，並保留可追蹤的錯誤與 log。
- `render_project()` 保留同步呼叫相容性，並可接收 Job execution context。
- 後端 API：
  - `POST /api/project/render-job`
  - `GET /api/render-job?id=...`
  - `GET /api/render-jobs?project_id=...`
  - `POST /api/render-job/cancel`
  - 舊有 `GET /api/jobs?project_id=...` 已保留並整合 Render Job。

## 主要檔案

- `src/video_vault/render_job_models.py`
- `src/video_vault/render_job_store.py`
- `src/video_vault/ffmpeg_process_runner.py`
- `src/video_vault/render_job_manager.py`
- `src/video_vault/render_job_api.py`
- `src/video_vault/project_renderer.py`
- `src/video_vault/segment_renderer.py`
- `src/video_vault/timeline_assembler.py`
- `src/video_vault/ui.py`
- `src/video_vault/config.py`

## Phase 4B commits

- `39a13d5` `feat: add persistent render job store and lifecycle`
- `dfde8e5` `feat: add managed ffmpeg process runner with progress`
- `d3e6c2c` `feat: add targeted render cancellation`
- `0ff0345` `feat: expose render job backend APIs`

## 本機驗證

- `pytest`：152 passed，1 warning。
- Render Job E2E：2 passed。
- Project Renderer E2E：1 passed。
- Segment Renderer E2E：1 passed。
- CLI tests：3 passed。
- `npm run build`：成功。
- `git diff --check`：成功。
- FFmpeg / FFprobe：8.1.2。
- Warning 為既有 Python 3.13 的 `cgi` deprecated warning，來源是 `src/video_vault/ui.py`。

## 明日建議順序

1. 先檢查 PR #6 的 diff、review comments 與 CI 狀態，再決定是否合併。
2. 啟動實際 WebUI，建立已核准專案，測試建立 job、輪詢進度、取消 job。
3. 確認 `/api/jobs` 與新 Render Job 資料的相容輸出。
4. 審查取消邊界：接近 publish、manager shutdown、服務重啟時的 process handle。
5. 視需要補上 Render Job CLI；Phase 4B 目前沒有新增 CLI。
6. 下一階段再做 React Render Job UI，顯示 status、stage、percent、current segment、encoder、output、error 與停止按鈕。

## 已知限制

- React Render Job UI 尚未接上；目前已完成後端 API 與持久化狀態。
- 尚未處理多 BGM 排程、轉場、overlay、分散式 worker 或 GPU 平行 job。
- Phase 4B 沒有改 Approval Gate，也沒有改正式剪輯流程契約。
- GitHub 自動 CI 狀態未作為本次驗證依據，以上為本機測試結果。

## 明日開始指令

```powershell
Set-Location 'C:\Users\b3b3b\Documents\Codex\AI剪片助手-render-jobs'
git checkout codex/render-v2-render-jobs
git status
git log --oneline -5
git fetch origin main
```

接著開啟 PR #6，確認 PR 分支相對最新 `main` 的變更，再進行實際 WebUI Render Job 驗證。
