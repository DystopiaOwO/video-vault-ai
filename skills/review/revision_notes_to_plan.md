# Revision Notes To Plan

## Purpose

把使用者退回修改的自然語言備註保留到下一版 plan，而不是直接覆蓋舊結果。

## Flow

1. 使用者在審核區輸入備註。
2. 系統寫入 `feedback/revision_notes.md` 與 timestamped revision 檔。
3. 重新產生 `project_plan.json`。
4. 新 plan 寫入 `revision_notes`、`feedback_applied`、`feedback_unresolved`。
5. plan version 增加，舊版保留在 `plans/`。
6. 專案回到 `needs_review`。

## Rule

備註只影響 planning/review，不可繞過 approval gate。
