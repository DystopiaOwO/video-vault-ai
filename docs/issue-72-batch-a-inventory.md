# Issue #72 Batch A 盤點

此文件是 Batch A 實作前的基線盤點，範圍限定 Phase 1～3；Phase 4～6（Batch B）不在本分支。

## Phase 1：Ownership、Perception、Stable Identity

### Completed

- `project_videos.project_media_uuid` 已是 project-local stable identity，且 project source 會複製到各自的 `08_projects/project_<id>/source`。
- `analysis_runs` 已有 run UUID、generation、input snapshot、staging path、previous successful run。
- 感知結果先寫入 run staging directory，發布後才替換 live `frames`／`segments`。
- 中止、失敗與服務重啟 recovery 已有 persisted status；stable segment UUID 與 temporal/semantic matching 已存在。
- `segment_state_migration.py` 已遷移 storyboard、timing、audio、color 與 review state，並產生 orphan/conflict report。

### Partial

- run-scoped staging 原本只有檔案；本 checkpoint 已補 durable `analysis_run_frames`／`analysis_run_segments`，並在 publish 前先寫入。
- project media relation 原本沒有 immutable source fingerprint 與 ownership marker；本 checkpoint 已補欄位與可重複 migration report。
- migration report 原本版本與 contract 不完整；本 checkpoint 已升為 stable-segment-state-v2 並保留 rollback snapshot。
- reanalysis 的 publish rollback 原本缺少 run provider contract、published revision 與 interrupted timestamp；本 checkpoint 已補入 `analysis_runs`。

### Missing

- 完整的跨程序 DB transaction/replay coordinator 仍留待後續 Render/Job 階段；本批已提供 durable staging record，但不擴張既有 publish engine。

## Phase 2：Target Duration、Visual Timeline

### Completed

- project 有 `target_duration_seconds`。
- storyboard 有 groups、included/order、locked/manual flags、thumbnail 與 notes。
- duration-budget-v1 已依既有 storyboard/manual input 做 deterministic selection，輸出 target、estimated、tolerance、group budget、coverage、omitted reason 與 locked conflict。
- visual-timeline-v1 已以 versioned `visual_items` 產生 chapter cards，並納入 project plan、Render Manifest、manifest hash 與 approval snapshot。

### Partial

- formal renderer 的 overlay composition、font/assets runtime validation 留待 Batch B；本批只建立可審核、可 hash 的 visual timeline contract。

### Missing

- Phase 2 Batch A 沒有未完成的核心資料契約；Batch B 的正式 overlay render 不在本分支。

## Phase 3：BGM License、整合驗證

### Completed

- global BGM library、project BGM relation、local import、source URL、license URL、attribution text 已存在。
- `attribution_status`、`license_status`、`license_verified_at`、`license_source_url`、`verification_source`、`verification_provenance` 已加入資料庫、API、manifest、approval snapshot 與 WebUI。
- legacy migration 對有可靠 CC0/public-domain/self-owned 證據的資料標為 `not_required/verified`，有可靠 CC BY 證據的資料標為 `required/verified`；沒有證據的 legacy 0 改為 `unknown/unverified`。
- manifest 產生 `bgm_credits` 與 `unresolved_bgm_licenses`；未知或未驗證授權預設使 approval fail closed，僅接受一次性、明確列出 track IDs 的 acknowledgement。

### Partial

- server-side BGM form 已可輸入授權 URL 與署名狀態；專用的 library 編輯/授權證據管理畫面仍可在後續 UI 工作補強。

### Missing

- Phase 3 Batch A 沒有未完成的核心授權資料與 gate 契約；更完整的線上驗證 provider 不在本批範圍。

## 採用方案

- 沿用既有 `analysis_runs`、`project_media_uuid`、segment identity migration、storyboard、approval snapshot 與 project BGM；不建立第二套 perception、timeline 或 approval 系統。
- Phase 1 先補 run-scoped DB staging 與 project media fingerprint metadata。
- Phase 2 以既有 project plan/storyboard 為輸入，新增 deterministic duration budget 與 versioned visual timeline projection。
- Phase 3 擴充既有 `bgm_tracks` 欄位並在既有 manifest/approval snapshot 契約中加入授權狀態。
