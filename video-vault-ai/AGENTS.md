# video-vault-ai 工作規則

- WebUI-first：主要工作流從 `python -m video_vault ui` 操作，CLI 只做 debug/helper。
- Project-first：一個 project 可以包含多支 clips，不要把單支影片視為完整專案。
- 先做 clip-level perception，再做 project-level story/plan。
- travel/outdoor 依 `time_of_day`、`activity`、clip order 整理段落。
- coffee/matcha/roasting 預設 `diary_montage`，不是教學、不是業配。
- 先產生 plan/story 給人審；未 approved 不 render。
- 每次修改後回到 `needs_review`。
- rendered 後再修改 plan 要建立新版本。
- 不要自動覆蓋原始素材。
- BGM 必須記錄來源與授權。
