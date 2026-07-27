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

- run-scoped staging 原本只有檔案；live DB 以 video 為 scope，沒有 durable frame/segment staging tables。
- project media relation 沒有保存 immutable source fingerprint，也沒有明確 ownership/migration schema marker。
- migration report 有 rollback snapshot，但版本與 migration contract 不完整。
- reanalysis 的 publish rollback 已存在，但 run provider contract、published revision 與 interrupted timestamp 未完整保存。

### Missing

- Batch A 所需的 project-local source fingerprint/ownership metadata contract。
- run-scoped DB staging rows 與 publish boundary 的 durable record。

## Phase 2：Target Duration、Visual Timeline

### Completed

- project 有 `target_duration_seconds`。
- storyboard 有 groups、included/order、locked/manual flags、thumbnail 與 notes。
- title card suggestion 已存在於 `project_plan.json`；Render Manifest 已包含 storyboard render state。

### Partial

- target duration 目前只被記錄，尚未真正影響自動選片、group budget、omitted reason 或 conflict。
- title cards 尚未是版本化 `visual_items` contract，也尚未完整進入 manifest/approval/cache/report。

### Missing

- deterministic duration budgeting、chapter coverage、target/estimated/tolerance API data。
- versioned visual timeline items 與 formal manifest integration。

## Phase 3：BGM License、整合驗證

### Completed

- global BGM library、project BGM relation、local import、source URL、license URL、attribution text 已存在。
- project plan、approval snapshot、manifest 可帶出 BGM 與署名資訊。

### Partial

- 目前只有 `attribution_required` boolean，沒有 `attribution_status`、`license_status`、verification time/provenance。
- legacy `attribution_required=0` 無法安全區分 CC0/public domain 與未知授權；manifest 只警告授權資料不完整。

### Missing

- `required/not_required/unknown` 與 `verified/unverified/invalid` contract。
- unknown BGM 不得被列入「不需署名」、approval/render checklist warning 與 YouTube credits/unresolved license 分流。

## 採用方案

- 沿用既有 `analysis_runs`、`project_media_uuid`、segment identity migration、storyboard、approval snapshot 與 project BGM；不建立第二套 perception、timeline 或 approval 系統。
- Phase 1 先補 run-scoped DB staging 與 project media fingerprint metadata。
- Phase 2 以既有 project plan/storyboard 為輸入，新增 deterministic duration budget 與 versioned visual timeline projection。
- Phase 3 擴充既有 `bgm_tracks` 欄位並在既有 manifest/approval snapshot 契約中加入授權狀態。
