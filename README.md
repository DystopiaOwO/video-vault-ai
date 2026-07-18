# video-vault-ai

Windows 本機用的 AI 影片素材管理與初剪工具。主流程是 WebUI-first：一個專案可以放多支影片，先做內容感知與故事整理，人工審核後才正式輸出。

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
python -m video_vault ui
```

打開 `http://127.0.0.1:8765/`：

1. 新增專案。
2. 匯入一支或多支影片。
3. 跑待感知素材，或全部重跑感知。
4. 產生故事整理，調整片段順序/取用/時間。
5. 核准專案。
6. 輸出 HyperFrames 初剪或 OpenCut 素材包。

正式輸出有 project-level approval gate；`needs_review` / `rejected` / `draft` 只能預覽與規劃，不能輸出 MP4 或調色片段。

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

## Human-in-the-loop Project Flow

新流程不會丟影片後直接剪完。WebUI 會保留：

- 專案素材：`08_projects/project_<id>/source`
- 專案計畫：`08_projects/project_<id>/project_plan.json`
- 故事整理：`08_projects/project_<id>/project_script.md`
- 審核狀態：`08_projects/project_<id>/review_status.json`
- 片段審核：`08_projects/project_<id>/feedback/segment_review.json`

## BGM Library

BGM 要先登錄到本地資料庫，保留來源和授權。WebUI 總覽在 `http://127.0.0.1:8765/bgm`，舊版上傳頁在 `http://127.0.0.1:8765/classic-bgm`。

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
