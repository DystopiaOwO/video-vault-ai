# Render Pipeline v2 共用契約

本文件定義 Render Pipeline v2 的跨模組資料邊界。渲染、工作佇列、API、WebUI 與相容性層都應使用 `src/video_vault/render_types.py` 的型別，不得各自建立第二套 Manifest、Job 或 Profile 型別。

## Wave 0 範圍

Wave 0 只建立資料契約，不改變現有內容感知、資料庫、故事規劃或輸出行為。後續代理可以在此契約上實作模組；若發現契約不足，先回報主代理，不能自行改出平行格式。

## 共用型別

`render_types.py` 提供：

- `RenderKind`：`rough_preview`、`accurate_preview`、`final`
- `RenderProfile`：已解析的輸出尺寸、FPS、像素格式、音訊與編碼器特性
- `RenderSettings`：Profile、編碼器、轉場、Overlay、音訊、BGM、調色設定
- `MediaProbeResult`：單支來源的 ffprobe 正規化結果
- `RenderManifest`：一次輸出的不可變輸入描述
- `RenderSegment`：Manifest 中的一個素材範圍與時間軸位置
- `BgmSettings`：BGM 檔案、音量、淡入淡出與授權資訊
- `ColorSettings`：調色模式、LUT、reference 與微調決策
- `RenderJob`：持久化工作狀態
- `RenderJobStatus`：`queued`、`running`、`completed`、`failed`、`failed_qc`、`cancelled`
- `RenderStage`：Manifest、preflight、probe、segment、assembly、audio、overlay、encode、QC 階段
- `QcReport`：輸出品質檢查結果

這些 dataclass 不執行 FFmpeg、不讀寫資料庫，也不自行排序素材。`to_dict()` 是唯一提供給 JSON 邊界的通用序列化入口。

## Manifest 不變性與 Hash

Manifest 編譯完成後即視為 render-time immutable。`manifest_hash` 應由不含 `created_at` 的穩定 JSON 內容計算；相同輸入與設定必須得到相同 Hash。任何會影響輸出的欄位變更都必須產生新 Hash，並讓既有 approval 失效。

正式輸出前必須同時確認：project approved、plan approved、review approved，以及目前 `plan_id` 和 `manifest_hash` 與核准時相同。未核准只允許 HTML／timeline／dry-run 等預覽與規劃輸出。

## JSON Schema

`schemas/render_manifest.schema.json` 是 `RenderManifest` 的機器可驗證邊界。Schema 目前採毫秒整數保存 source 與 timeline 時間，避免浮點剪點在跨模組傳遞時失真；`speed` 仍保留為正數浮點。

## 輸出目錄

每個 project 的 Render v2 產物使用獨立目錄，不寫回原始素材：

```text
08_projects/project_<project_id>/render/
├─ manifest/render_manifest.json
├─ jobs/<job_id>.json
├─ cache/segments/<cache_key>.mp4
├─ cache/segments/<cache_key>.json
├─ cache/segments/<cache_key>.log
├─ outputs/<plan_id>_<manifest_hash>.<ext>
└─ qc/<job_id>/qc_report.json
```

未來實作若需要暫存檔，應使用同一 project render 目錄下的 `.partial` 檔案；QC 通過後才可改成正式輸出名稱。

## Render Profile 名稱

第一版固定提供以下名稱，Profile 解析結果不得因來源影片格式自行改變：

```text
preview_1080p30
preview_1080p60
final_1080p30
final_1080p60
final_2160p30
final_2160p60
```

Profile registry 負責將名稱解析為尺寸、FPS、pixel format、48 kHz stereo 音訊、主要 encoder 與 CPU fallback encoder。無效名稱應明確失敗。

## Job API 契約

Render API 的所有 request 都必須包含 `project_id`，非同步動作回傳 `job_id`，不可在 HTTP thread 內阻塞等待 FFmpeg：

```text
POST /api/project/render/settings
GET  /api/project/render/settings
POST /api/project/render/compile
POST /api/project/render/validate
POST /api/project/render/preview
POST /api/project/render/final
POST /api/project/render/cancel
GET  /api/project/render/jobs
GET  /api/project/render/job
GET  /api/project/render/outputs
```

共用 request 形狀：

```json
{
  "project_id": 1,
  "settings": {},
  "job_id": "optional-for-cancel"
}
```

成功回應至少包含：

```json
{
  "ok": true,
  "job_id": "job-uuid",
  "manifest_hash": "sha256-or-equivalent",
  "message": ""
}
```

錯誤回應至少包含：

```json
{
  "ok": false,
  "error": {
    "code": "approval_required",
    "message": "Project must be approved before final render",
    "details": {}
  }
}
```

這份 API 契約不刪除既有路由；新路由由 Backend API 代理以相容方式加入。

## 代理邊界

- Foundation 產生 Profile、Probe、Manifest；不負責執行渲染。
- Segment Renderer 只處理單段標準化與 cache；不自行決定排序或組合 timeline。
- Timeline Assembler 只讀 Manifest 順序；不重新建立排序規則。
- Job／Process 層只管理指定 Job 的 process tree；禁止全域終止 FFmpeg。
- API／Approval 層負責 preflight 與 gate；不能由 UI 狀態代替 server-side check。
- HyperFrames／OpenCut 是相容性輸出；不得繞過正式輸出的 approval gate。

## 相容性原則

現有 CLI、內容感知、Story Plan、BGM Library、Color Preview、OpenCut handoff 與 HyperFrames HTML preview 必須保持可用。Render v2 是新增共用管線，不應在 Wave 0 直接替換舊流程。
