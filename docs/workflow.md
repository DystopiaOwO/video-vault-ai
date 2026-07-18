# Workflow

目前主流程以 WebUI 專案為中心：

1. 啟動：`python -m video_vault ui`
2. 打開：`http://127.0.0.1:8765/`
3. 新增專案。
4. 匯入一支或多支素材。
5. 跑內容感知。
6. 產生故事整理。
7. 在片段審核表調整保留、順序、起訖時間與備註。
8. 指定 BGM，需要時到 `/bgm` 看本地資料庫。
9. 核准專案。
10. 輸出 HyperFrames 初剪 MP4 或 OpenCut handoff / 調色片段。

正式輸出需要 approval gate 通過；未核准專案仍可做故事整理、HTML timeline、OpenCut handoff 和調色預覽。

背景工作狀態在 WebUI 內顯示百分比；目前狀態存在記憶體，重啟 UI 後會清空。

## OpenMontage-style Skeleton

專案詳情會回傳 `workflow` 骨架，固定階段為 `import → perception → story → review → handoff → render`。第一版只標示階段狀態與 artifact 路徑，不新增剪輯引擎。
