# Issue #72 Batch B completion

此批接續 PR #74，補齊 Batch B 原先標示為 deferred 的正式 visual overlay composition，並完成輸出、儲存空間與本機交接操作的 hardening。

## Formal visual composition

- `visual-timeline-v1` 的 chapter cards 會依核准後、含 speed 調整的 segment timeline 重新對齊。
- 分鏡群組標題會成為正式輸出的字卡內容；修改標題會使既有核准失效。
- 被排除或沒有可輸出片段的群組不會留下 0 秒 visual item。
- 每個 visual item 在 Manifest／approval snapshot 中固定具體字型檔；缺少、變更或不支援的 style／animation 均 fail closed。
- FFmpeg 使用 UTF-8 text files 合成字卡，支援中文、apostrophe 與特殊路徑；成功、失敗或取消後會清理暫存文字。
- 有字卡時依核准 encoder contract 重新編碼視訊；無字卡時維持既有 stream-copy 路徑。
- Render Report 記錄 visual contract、item IDs、合成狀態與磁碟 preflight。

## Output and audio correctness

- 無 BGM 與有 BGM 的正式輸出均套用同一 visual filter。
- BGM／原音混音會 pad、trim 並重設時間戳至核准 timeline 長度。
- Final QC 的 A/V tail tolerance 依 frame duration 計算，仍會拒絕實質漂移。
- Render Manifest 補上 segment group identity，讓 handoff coverage 與 visual alignment 可稽核。

## Retention and failure recovery

- Cleanup 支援 keep-last-N、cache size cap、minimum free disk 與 LRU 候選排序。
- 每刪除一個 artifact 就立即 journal inventory；中途中斷後可由 reconcile 恢復。
- reconcile 會標記 inventory 中已消失的檔案，不再把它們視為 active。
- 正式輸出在啟動 FFmpeg 前估算暫存／輸出需求；空間不足時提早 fail closed。
- Storage API／WebUI 顯示可用空間與恢復數量。

## Local WebUI hardening

- JSON、form 與 multipart upload 在讀取 body 前驗證 `Content-Length` 上限。
- 上傳使用有界 semaphore，忙碌時明確拒絕，不建立無上限背景工作。
- 啟動 OpenCut 或開啟本機資料夾前要求明確確認。
- 已確認的本機操作寫入專案 `decisions/local_actions.jsonl` 稽核紀錄。

## Verification

- Python：`434 passed`
- Visual／Manifest／real FFmpeg targeted regression：`33 passed`
- Frontend：`30 files / 223 tests passed`
- Frontend production build：passed
- `python -m compileall -q src tests`：passed
- `git diff --check`：passed

Windows authoritative smoke 與跨平台 CI 仍以 PR #74 最新推送後的 GitHub Actions 結果為準。
