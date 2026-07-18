# Review Gate

## Purpose

保護正式輸出，避免尚未審核的故事計畫直接產生成片。

## Required Checks

- project 存在。
- `project.status == approved`
- `review_status.json` 存在。
- `review_status.json.approved_by_user == true`
- `project_plan.json.status == approved`

## Allowed Before Approval

- 內容感知
- 故事整理
- revision notes
- segment review
- HTML timeline / handoff
- color preview

## Blocked Before Approval

- HyperFrames MP4
- OpenCut graded clips
- 任何正式 render

## Implementation Note

共用 `can_project_render()` / `assert_project_approved()`，不要在各輸出入口重寫 gate。
