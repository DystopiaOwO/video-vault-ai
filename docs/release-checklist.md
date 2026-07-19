# Release Checklist

這份清單用來確認本機版或 GitHub release 前，環境、測試與輸出流程都可重現。

## Environment

- [ ] 使用 `python -m video_vault doctor` 檢查必要環境。
- [ ] `doctor --json` 可被腳本解析，且不建立資料庫或修改素材庫。
- [ ] `ffmpeg` 與 `ffprobe` 可執行，版本符合目前測試範圍。
- [ ] `config.yaml` 指向預期的 library root，資料庫位於 `05_index`。
- [ ] 原始素材位於專案 `source` 或素材庫，輸出不覆蓋原始檔。

開發環境再執行：

```powershell
python -m video_vault doctor --dev
```

## Verification

```powershell
pytest -q
python scripts/check_encoding.py
git diff --check

Set-Location web
npm ci
npm test
npm run build
```

## Human approval

- [ ] 每個正式輸出專案都有 `review_status.json`，且 `approved_by_user` 為 true。
- [ ] `project_plan.json` 狀態為 `approved`。
- [ ] Approval gate 通過目前 Manifest hash 驗證。
- [ ] 修改片段、BGM、調色、輸出設定後重新審核。

## Output smoke check

- [ ] 建立一個含兩支來源影片的測試專案。
- [ ] 確認 Render Job 由 queued 進入 running，再進入 succeeded 或清楚的失敗狀態。
- [ ] 確認輸出 MP4 可由 `ffprobe` 讀取，且 render report 存在。
- [ ] 確認來源檔案 SHA-256 不變。
- [ ] 再次輸出時確認 segment cache 命中，且沒有殘留 `.partial.mp4` 或 metadata temp。
- [ ] 取消一個執行中的 Job，確認只停止指定 Job 的 process tree。

CI 應在 pull request 與 `main` push 執行；CI 不提交 `web/dist`、render outputs、cache 或使用者素材。
