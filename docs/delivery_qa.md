# Formal Delivery QA

正式輸出成功只代表 render artifact 已原子發布，不代表影片已可交付。每個成功的 Render Job 會建立獨立、版本化的 Delivery QA run：

```text
Approved Render -> Automated Delivery QA -> Evidence Bundle -> Human Final Preview -> Deliverable Ready
```

## 狀態與人工邊界

- `needs_qa`：尚無 current QA，或核准快照／輸出 fingerprint 已變更。
- `qa_blocked`：至少一項檢查為 `blocked`；`skipped` 也不可由人工直接繞過。
- `qa_needs_review`：自動檢查完成，仍需完整觀看正式成片。
- `deliverable_ready`：本機使用者已明確確認預覽；每個 warning 都有理由、操作者與時間。

QA review 使用獨立 `review_version` 做 optimistic concurrency。Review 不修改 Render Manifest、approved snapshot、source media 或 project revision。重新 Render 一定建立新的 QA run；「重新檢查」只重跑目前正式輸出的 QA，不會自動重送 Render。

## Evidence 與安全性

每個 run 位於 `08_projects/project_<id>/qa/<qa_run_uuid>/`，包含 `report.json`、`REPORT.md`、overview contact sheet、章節 contact sheets、finding event strips、audio summary 與 artifact index。API 與 WebUI 只暴露 stable IDs、timecodes、fingerprints 和去敏後 metrics；report 不包含來源檔名、絕對路徑、憑證或原素材副本。

Delivery QA 對正式 Render Report fail closed：Report 必須存在且可解析，`manifest_hash`、`output_sha256` 必須與核准 manifest／實際輸出 exact match，且 `qc.passed`、full decode 與 timestamp continuity 都必須明確為 `true`。缺失、空白或損壞 provenance 一律是 `blocked`。

Flash 檢查使用正式輸出的 FFmpeg `signalstats` brightness samples，只有快速 brightness reversal 才形成 timestamped flash events；scene cut 不會直接當成 flash，片頭／片尾 fade-to-black 仍是獨立 finding。Audio 檢查會重新計算核准 manifest 每個 segment 的 cache key，並比對 Render Report 的 segment keys、BGM used 與 fingerprint；role、fade 或 BGM provenance 不一致一律 blocked。

## Profiles

`travel_diary`、`coffee_matcha_diary`、`roasting_diary`（映射至 coffee/matcha QA 門檻）與 `general_diary` 使用不同 freeze/silence/repeat thresholds。全域、profile 與 project override 都會在每項 check 的 `threshold_source` 留下 resolved value 與來源；known malformed／negative／NaN／Inf 與 unknown threshold key 會保留 structured audit 並阻止 `deliverable_ready`，不會靜默 fallback。

## Attribution

此設計在概念上參考 Hao0321/video-autopilot-kit commit `f081e99ed169b5aba1156a2aa6a80b748c39fbc9` 的 delivery QA、report shell、contact-sheet 與重複素材稽核方向。VID-7 實作為本專案獨立撰寫，未複製或改寫上游程式碼；因此屬於 concept inspired by，沒有 code adapted from。
