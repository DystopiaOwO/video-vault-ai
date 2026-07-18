# video-vault-ai

Windows 本機用的 AI 影片素材管理與初步內容感知工具。先做能跑的核心流程：掃描、匯入、metadata、抽幀、proxy、SQLite index、mock 分析、Markdown 報告。

## 安裝

```powershell
cd video-vault-ai
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

請先安裝 FFmpeg，並確認 `ffmpeg` / `ffprobe` 在 PATH 裡。

## 快速開始

```powershell
python -m video_vault init
python -m video_vault scan
python -m video_vault ingest
python -m video_vault perceive
python -m video_vault draft-plan
python -m video_vault review-plan --video-id 1
python -m video_vault approve-plan --video-id 1
python -m video_vault render-approved --video-id 1
```

`render-approved` 只會 render 已核准的 `edit_plan.json`。未審核或被退回的 plan 只會停在建議階段。

Render Pipeline v2 相容層會優先使用人工審核後的 Render Manifest：HyperFrames 保留 `source_in` 與 Manifest 順序，OpenCut handoff 保留 `order`、速度、音訊角色與取用狀態。JSON／CSV／README handoff 可在審核階段產生；graded clips 與正式 MP4 仍受 project approval gate 保護。遷移細節請見 `docs/render-v2/migration.md`。

預設資料庫在 `D:/VideoLibrary/05_index/video_vault.sqlite3`，報告輸出到 `D:/VideoLibrary/06_reports`。

## 設定

修改 `config.yaml`：

```yaml
library_root: "D:/VideoLibrary"
frame_interval_seconds: 5
default_ingest_mode: "copy"
ffmpeg_path: "ffmpeg"
ffprobe_path: "ffprobe"
ai:
  provider: "mock"
```

`mock` provider 不呼叫 API，適合先驗證 pipeline。要改用雲端 Vision：

```yaml
ai:
  provider: "cloud"
  cloud:
    provider: "openai"
    model: "gpt-4.1-mini"
    api_key_env: "OPENAI_API_KEY"
```

每次只上傳抽出的 frame，不上傳整支影片；raw response 快取在 `05_index/raw_ai_outputs`。

## Human-in-the-loop Planner

新流程不會丟影片後直接剪完：

```powershell
python -m video_vault perceive
python -m video_vault draft-plan
python -m video_vault review-plan --video-id 1
python -m video_vault approve-plan --video-id 1
python -m video_vault render-approved --video-id 1
```

每支影片會產生：

- `05_index/video_<id>/perception.json`
- `05_index/video_<id>/edit_plan.json`
- `05_index/video_<id>/edit_script.md`
- `05_index/video_<id>/review_status.json`

退回修改：

```powershell
notepad D:/VideoLibrary/05_index/video_1/revision_prompt.txt
python -m video_vault revise-plan --video-id 1
```

## BGM Library

BGM 要先登錄到本地資料庫，保留來源和授權：

```powershell
python -m video_vault add-bgm D:/music/song.mp3 `
  --title "Song title" `
  --artist "Artist" `
  --source-url "https://..." `
  --license-name "CC BY 4.0" `
  --license-url "https://creativecommons.org/licenses/by/4.0/" `
  --attribution-required

python -m video_vault list-bgm
python -m video_vault bgm-credits
```

可找 BGM 的入口：

- Pixabay Music: https://pixabay.com/music/
- YouTube Studio Audio Library: https://support.google.com/youtube/answer/3376882
- Free Music Archive: https://freemusicarchive.org/

每首歌以該曲頁面授權為準；上傳 YouTube 前請把 `bgm-credits` 輸出貼到說明欄。

## 測試

```powershell
pytest
```
