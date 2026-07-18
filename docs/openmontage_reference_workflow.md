# OpenMontage Reference Workflow

這份文件只定義 workflow skeleton；不接外部素材、不接雲端 provider、不改正式 render 行為。

## Core Stages

1. `import`
   - 收集專案素材。
   - Artifact: `08_projects/project_<id>/source`
2. `perception`
   - 對抽出的 frame 做內容感知。
   - Artifact: `clips/*/clip.json`, SQLite `frames`, `segments`
3. `story`
   - 依內容分組、產生故事整理與字卡建議。
   - Artifact: `project_plan.json`, `project_script.md`
4. `review`
   - 人工審核、備註、片段保留/排序/時間微調。
   - Artifact: `review_status.json`, `feedback/*.json`, `feedback/*.md`
5. `handoff`
   - 只產生剪輯交接檔與可預覽 timeline。
   - Artifact: `output/opencut_handoff`, `output/hyperframes`
6. `render`
   - 只有 approval gate 通過後才允許正式輸出。
   - Artifact: final MP4 / graded clips

## Gate Rule

`needs_review` / `rejected` / `draft` 只能做 preview、planning、handoff。正式 MP4、graded clips、任何改變剪輯結果的輸出都必須通過 project-level approval gate。

## Current Scope

- 保留現有本機 provider 與 mock provider。
- 不新增雲端 provider。
- 不下載外部素材。
- 不改 HyperFrames / OpenCut render 行為。
