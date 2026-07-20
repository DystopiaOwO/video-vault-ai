# PR #9 色彩一致性工作交接

## 工作位置

- Repository：`DystopiaOwO/video-vault-ai`
- PR：`#9 Add project color consistency workflow and previews`
- PR URL：`https://github.com/DystopiaOwO/video-vault-ai/pull/9`
- Branch：`codex/color-consistency`
- Base：`main`
- 目前 PR 維持 Draft，尚未合併。
- 目前 HEAD：`84665a3886a68d796546833a472b704e0bba3779`
- 目前工作樹有未提交修改，尚未 push。

## 本輪已完成

### CI process-group 測試穩定化

- 已查看 Ubuntu Python 3.13 CI run `29693954323`、job `88211387107`。
- 原因：`test_independent_process_group_survives_runner_cancel` 在取消後立即檢查 helper PID，發生程序狀態轉換競態。
- 測試 helper 改為忽略 SIGTERM/SIGHUP。
- 新增有 deadline 的 `_wait_for_pid_running()`，POSIX 會排除 zombie state。
- cleanup 對已消失的 PID 使用 `ProcessLookupError` 保護。
- 沒有修改正式 process runner，也沒有改成全域終止 FFmpeg。

### Project color consistency

- 新增／擴充 `src/video_vault/color_consistency.py`。
- 建立 project-level color state，包含：
  - project enabled
  - reference clip/frame
  - project suggested/applied
  - per-segment enabled/locked/excluded
  - per-segment suggested/applied
  - confidence/warnings/reference_candidate
- `locked=true` 仍會套用該 segment 的 applied settings。
- `excluded=true` 或 segment disabled 時有效 color mode 為 `none`，不參與自動 reference 選擇。
- force analysis 保留 locked segment 的 suggested/applied。
- 低信心分析不會自動寫入新的 segment applied 值。

### Reference frame / analysis

- Reference frame 會寫入：
  - `08_projects/project_<id>/color/reference.json`
  - `08_projects/project_<id>/color/reference_frames/`
- 使用 FFmpeg 擷取指定 timestamp，會檢查 source 存在與 timestamp 範圍。
- 失敗回傳結構化 `ColorReferenceError` code，不再靜默產生假 128 fallback。
- 分析包含亮度統計、percentile、RGB 平均、白平衡傾向、飽和度傾向、裁切警告與 sampled counts。
- API 回傳會移除本機絕對 `frame_path`，改提供安全的 `frame_url`；analysis.reference 也同步清理。
- 新增安全的 preview/reference file endpoint，阻擋 path traversal。

### Preview / WebUI

- Before/After 預覽改為以 project segments 為單位，預設約 4 秒，並以 segment timestamp 為起點。
- Before 使用 `mode=none`，After 使用 segment effective applied settings。
- 一般預覽沿用快取；提供明確的「強制重新產生」按鈕。
- WebUI 顯示 reference thumbnail、confidence、warnings、per-segment Lock/Exclude/Enable、套用建議、重設為專案與色彩數值。
- Preview 使用 `<video controls>`。
- 預覽失敗會清除 before/after partial files，不留下半成品。
- 色彩 API 網路例外會顯示錯誤並解除 loading；預覽失敗會清除舊預覽，避免畫面誤顯示上一輪結果。
- Exclude/Disable 的片段會標示「目前不套用片段色彩」，並停用無效的套用/重設/數值編輯操作。
- highlights/shadows 已有中文 UI 標籤「高光／陰影」。

### Color pipeline / renderer / cache

- Filter 順序固定為：
  - technical LUT
  - exposure
  - white balance
  - contrast/gamma
  - highlights/shadows
  - saturation
  - resize/pad/fps/pixel format
- Segment renderer 在 trim/setpts/speed 後套用色彩，再做輸出格式標準化。
- LUT 缺失會產生分析 warning，preview 會回傳 structured error；Manifest validation 也會在正式執行前擋下不存在的 LUT。
- Segment cache 已包含 resolved LUT path、size、mtime、SHA-256、pipeline version。
- Segment cache 也包含來源影片 SHA-256；即使來源內容被替換但 size/mtime 恰好相同，也不會誤用舊 cache。
- cache color settings 已補上 highlights/shadows。
- preview cache 也會追蹤 LUT path、size、mtime、SHA-256；只變更 reference 而未套用建議時，不會讓所有 segment preview 失效。
- 修正 highlights/shadows curves 端點公式，避免正值把白點錯算為 0。
- color preview/job API 不回傳來源影片的本機絕對路徑，只保留安全的檔名與 preview URL。

## 已驗證結果

以下結果是在目前未提交修改中取得：

- 針對性 color/manifest/renderer/cache：`37 passed`
- `tests/test_ffmpeg_process_runner.py`：`6 passed, 2 skipped`
- `tests/test_render_job_e2e.py`：`3 passed`
- 前端 `npm ci`：成功
- 前端 `npm test`：`13 passed`
- 前端 `npm run build`：成功
- `PYTHONPATH=src py -3.10 scripts/check_encoding.py`：成功，無輸出錯誤
- 工作樹 `git diff --check`：成功
- 實際 FFmpeg filter graph（含 LUT/曝光/白平衡/對比/highlights/shadows/飽和）：成功
- 最終完整 pytest：`213 passed, 2 skipped`
- 最終前端 `npm test`：`13 passed`
- 最終前端 `npm run build`：成功

中途曾中止一次完整 pytest；該次殘留的本次 pytest 父子程序已確認並停止，之後已重新完整執行並通過。

## 目前未完成

1. 確認 `git diff --check origin/main...HEAD`；提交後再執行一次，因為該指令要檢查 PR diff。
2. 檢查完整 diff 後建立 commit。
3. Push 到既有 `codex/color-consistency` branch。
4. 更新 PR #9 body，保留 Draft，不要建立新 PR、不要合併。
5. 等待 GitHub Actions：Windows Python 3.11、Ubuntu Python 3.13、Frontend。
6. 若 CI 失敗，先讀完整 job log，再修正根因；不要任意 skip。

## 明天建議接續指令

```powershell
cd C:\Users\b3b3b\Documents\Codex\AI剪片助手-render-jobs

pytest -q
pytest -q tests/test_color_consistency.py tests/test_segment_renderer.py tests/test_render_manifest.py
$env:PYTHONPATH = "src"
py -3.10 scripts/check_encoding.py
git diff --check
git diff --check origin/main...HEAD

cd web
npm ci
npm test
npm run build
```

## 提交前檢查

- 只提交本次 PR #9 色彩一致性與 CI 測試穩定化相關檔案。
- 不修改 PR #8。
- 不新增 PR。
- 不合併 PR #9。
- 不將 PR #9 轉為 Ready for review。
- 不修改原始素材。
- 不繞過 approval gate。
