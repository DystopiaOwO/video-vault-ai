# API Contract

第一版 React WebUI 只用既有本機 API。

- `GET /api/projects`：專案列表。
- `GET /api/project?id=<id>`：專案詳情、clips、BGM、script、render gate。
- `GET /api/jobs?project_id=<id>`：目前記憶體中的工作狀態。
- `GET /api/bgm`：BGM 資料庫。
- `POST /api/projects`：建立專案，`{"name": "...", "video_ids": [1, 2], "category": "travel", "content_type": "diary_montage", "platform": "YouTube", "target_duration_seconds": 60}`
- `POST /api/project/upload`：匯入多支素材到指定專案，`multipart/form-data`。
- `POST /api/project/approve`：`{"project_id": 1, "notes": "..."}`
- `POST /api/project/reject`：`{"project_id": 1, "notes": "..."}`
- `POST /api/project/revise`：儲存備註並重建故事整理，`{"project_id": 1, "notes": "..."}`
- `POST /api/project/analyze`：`{"project_id": 1, "force": false}`
- `POST /api/project/analyze-job`：背景內容感知，`{"project_id": 1, "force": false}`
- `POST /api/project/analyze-video`：單支素材感知，可覆寫本次 sampling，`{"project_id": 1, "video_id": 2, "sampling": {"mode": "adaptive", "preset": "dense", "baseline_interval_seconds": 3, "max_frames_per_clip": 240}}`
- `POST /api/project/cloud-review/plan`：建立低信心／規則衝突／使用者指定的 cloud review preflight；只回傳選定抽幀數、provider、估算成本與 privacy contract，不上傳素材，`{"project_id": 1, "window_uuids": ["window_..."]}`
- `POST /api/project/cloud-review`：依 preflight 選定 window 送 optional cloud review，必須傳 `base_revision`；每 clip/project 的 call、frame、estimated-cost caps 由設定限制，失敗時保留 local result 並標記 needs-review。
- `POST /api/project/build-plan`：`{"project_id": 1}`
- `POST /api/project/segments`：儲存片段審核、順序與時間微調，`{"project_id": 1, "segments": [...]}`
- `POST /api/project/bgm`：把 BGM 加到專案，`{"project_id": 1, "bgm_id": 1}`
- `POST /api/project/color-preview`：產生調色預覽，`{"project_id": 1, "mode": "safe_restore"}`
- `POST /api/project/opencut-export`：`{"project_id": 1, "render_clips": false, "max_segments": 20}`
- `POST /api/project/opencut-job`：背景產生 OpenCut handoff / 調色片段，`{"project_id": 1, "render_clips": true, "max_segments": 20}`
- `POST /api/project/hyperframes-export`：同步產生 HyperFrames 專案 / MP4，`{"project_id": 1, "render": false, "max_segments": 20}`
- `POST /api/project/hyperframes-job`：背景產生 HyperFrames 專案 / MP4，`{"project_id": 1, "render": true, "max_segments": 20}`
- `POST /api/project/stop-jobs`：停止目前背景工作，`{"project_id": 1}`

正式輸出仍由 project approval gate 控制；未核准只能做 preview / handoff。
`project_plan.json` 會包含 `bgm_recommendations`，依故事分組推薦合適 BGM。
