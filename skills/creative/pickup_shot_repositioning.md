# Pickup Shot Repositioning

## Purpose

處理補拍鏡頭，例如餐廳外觀、路口、招牌、手部特寫。這些素材常常拍攝時間較晚，但故事上應該放到較早的位置。

## Heuristic

- 如果備註提到「補」、「外觀」、「招牌」、「入口」、「側面」，優先視為 pickup shot。
- pickup shot 可移到同地點章節開頭或轉場前。
- 不自動刪除原片段，只調整建議順序。
- 無法判斷時寫入 `feedback_unresolved`。

## Output

- 更新 segment review 建議順序。
- 在故事整理中標出「補拍鏡頭建議位置」。
- 專案保持 `needs_review`，等使用者核准。
