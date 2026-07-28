import { useEffect, useState } from "react";
import { api, type CleanupPlan, type StorageArtifact, type StorageSummary } from "../../api";

function bytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export function StorageWorkspace({ projectId, setMessage }: { projectId: number; setMessage: (value: string) => void }) {
  const [summary, setSummary] = useState<StorageSummary | null>(null);
  const [plan, setPlan] = useState<CleanupPlan | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      setSummary(await api.storage(projectId));
    } catch (error) {
      setMessage(`儲存空間載入失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  useEffect(() => { void refresh(); }, [projectId]);

  async function dryRun() {
    setBusy(true);
    try {
      const result = await api.storagePlan(projectId);
      if (!result.ok || !result.plan) throw new Error(result.error || "無法建立清理計畫");
      setPlan(result.plan);
      setMessage(`清理預覽：${result.plan.candidate_count} 個候選，可釋放 ${bytes(result.plan.candidate_size)}`);
    } catch (error) {
      setMessage(`清理預覽失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally { setBusy(false); }
  }

  async function execute() {
    if (!plan) return;
    setBusy(true);
    try {
      const result = await api.storageCleanup(projectId, plan.plan_id);
      if (!result.ok) throw new Error(result.error || "清理未完成");
      setMessage(`清理完成：釋放 ${bytes(result.reclaimed_bytes || 0)}`);
      setPlan(null);
      await refresh();
    } catch (error) {
      setMessage(`清理失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally { setBusy(false); }
  }

  async function togglePin(artifact: StorageArtifact) {
    try {
      const result = await api.storagePin(projectId, artifact.artifact_id, !artifact.pinned);
      if (!result.ok) throw new Error(result.error || "釘選狀態更新失敗");
      await refresh();
    } catch (error) {
      setMessage(`釘選失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  return <section className="workspace-section storage-workspace" id="workspace-storage" tabIndex={-1}>
    <div className="section-heading"><div><h3>儲存空間與清理</h3><p>只清理未被來源、核准、正式輸出或使用者釘選引用的 artifact。</p></div><button type="button" onClick={() => void dryRun()} disabled={busy}>建立清理預覽</button></div>
    {summary && <div className="project-metrics"><div><span>總使用量</span><b>{bytes(summary.total_bytes)}</b></div><div><span>磁碟可用</span><b>{bytes(summary.free_bytes)}</b></div><div><span>受保護</span><b>{bytes(summary.protected_bytes)}</b></div><div><span>Artifact</span><b>{summary.artifacts.length}</b></div><div><span>已釘選</span><b>{summary.pinned_count}</b></div></div>}
    {summary && summary.recovered_count > 0 && <div className="notice" role="status">已復原 {summary.recovered_count} 筆上次中斷後的 artifact 狀態；未自動刪除任何檔案。</div>}
    {plan && <div className="notice" role="status"><span>候選 {plan.candidate_count} 個，預估可釋放 {bytes(plan.candidate_size)}。</span><button type="button" onClick={() => void execute()} disabled={busy || plan.candidate_count === 0}>執行這份清理計畫</button></div>}
    <div className="storage-list">{(summary?.artifacts || []).slice(0, 30).map((artifact) => <div className="item" key={artifact.artifact_id}><div><b>{artifact.type}</b><span> · {bytes(artifact.size)} · {artifact.lifecycle_state}</span></div><div className="row"><span className="muted">{artifact.pinned ? "已釘選" : artifact.references?.length ? "有引用" : "未引用"}</span><button type="button" onClick={() => void togglePin(artifact)}>{artifact.pinned ? "解除釘選" : "釘選輸出"}</button></div></div>)}</div>
  </section>;
}
