# 專案音訊混音流程

## 狀態檔

每個專案可有 `audio_settings.json`，目前 schema version 為 `1`。它只保存使用者可調整的音訊設定：

- 原音角色：`keep`、`lower`、`mute`、`bgm_only`
- 每段音量與淡入／淡出
- 單一 BGM 的選擇、起始位置、音量、循環與淡入／淡出
- 專案音量正規化目標

後端保留本機 BGM 路徑與授權資料。WebUI 只收到 BGM 的公開資訊，不會收到 `source_path`。

## 預覽與正式輸出

`POST /api/project/audio-preview` 會依目前尚未核准的 Manifest 產生預覽，結果快取在：

```text
08_projects/project_<id>/output/audio_previews/
```

預覽會重用 Segment Cache，再用單一 BGM 做 trim／loop、淡入／淡出與混音；啟用正規化時使用 `loudnorm`。預覽不需要 approval gate。

正式輸出仍由既有 Render Job 與 approval gate 控制。音訊設定會寫入 Render Manifest，因此變更 BGM、角色、剪輯音量、淡化或正規化設定會改變 Manifest hash，舊核准自然失效。

## 相容性

舊資料中的 `keep_original`、`lower_original`、`mute` 會在音訊管線中轉換為新角色。沒有 `audio_settings.json` 的舊專案仍使用既有 `render_settings.json` 與 project BGM 設定。

## 限制

- 第一版只支援一首專案 BGM。
- `loudnorm` 使用單次濾鏡；完整兩階段 loudness 分析留待後續版本。
- 不在原始素材上寫入任何音訊處理結果。
