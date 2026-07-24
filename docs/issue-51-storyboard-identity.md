# Storyboard 分組 Identity

Storyboard 分組的 `group_id` 是資料識別，不是顯示名稱。重新命名、排序與收合都不會產生新的 identity；新建立的自訂分組會先使用 UUID 型 id，並由伺服器原樣保存。

舊版資料若沒有 `group_id`，前端會在載入與儲存前執行相容 migration。fallback id 由分組的穩定欄位與所屬片段 id 做 deterministic hash，並且不使用目前陣列 index 或單獨使用 title。這讓不同成員片段的同名 legacy 分組可以分開辨識，且重新載入、改名與重排仍可沿用相同 id。

若 legacy 檔案包含完全相同、沒有成員且沒有任何其他穩定識別欄位的重複物件，來源資料本身沒有足夠資訊區分兩個 identity；migration 會以 deterministic collision suffix 保持本次資料唯一，後續儲存後由 `group_id` 成為正式識別。新資料不應再省略 `group_id`。

分組 reorder 只在完整未篩選的清單啟用。搜尋或納入狀態篩選期間，為避免以可見子集誤動隱藏分組，UI 會停用上下移並提示先清除篩選；收合不算篩選，仍以完整 group order 計算。
