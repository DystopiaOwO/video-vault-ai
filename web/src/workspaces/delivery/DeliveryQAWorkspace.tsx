import { useEffect, useMemo, useRef, useState } from "react";

import { api, formatApiError, type DeliveryQACheck, type DeliveryQAState } from "../../api";
import { refreshFailureMessage, type ProjectMutationControls } from "../../projectMutation";
import "./delivery-qa-workspace.css";

type Props = {
  projectId: number;
  deliveryQA?: DeliveryQAState;
  setMessage: (message: string) => void;
  refreshProject: (options?: { forceFresh?: boolean }) => Promise<unknown>;
  mutationControls: ProjectMutationControls;
};

const STATUS_ORDER: Record<string, number> = { blocked: 0, warning: 1, skipped: 2, pass: 3 };

function firstEventTime(check: DeliveryQACheck): number | null {
  const rows = Array.isArray(check.metrics?.events) ? check.metrics.events : [];
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const candidate = (row as Record<string, unknown>).start_seconds ?? (row as Record<string, unknown>).timestamp_seconds;
    if (typeof candidate === "number" && Number.isFinite(candidate)) return candidate;
  }
  return null;
}

function statusLabel(value: string): string {
  return ({ pass: "通過", warning: "需人工確認", blocked: "封鎖", skipped: "未驗證" } as Record<string, string>)[value] || value;
}

function lifecycleLabel(value: string): string {
  return ({ needs_qa: "等待 QA", qa_blocked: "QA 封鎖", qa_needs_review: "等待人工最終預覽", deliverable_ready: "可交付" } as Record<string, string>)[value] || value;
}

export function DeliveryQAWorkspace({ projectId, deliveryQA, setMessage, refreshProject, mutationControls }: Props) {
  const qa = deliveryQA;
  const [warningReasons, setWarningReasons] = useState<Record<string, string>>({});
  const [reviewReason, setReviewReason] = useState("");
  const [previewConfirmed, setPreviewConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const runUuid = qa?.qa_run_uuid || "";

  useEffect(() => {
    setWarningReasons({});
    setReviewReason("");
    setPreviewConfirmed(false);
  }, [runUuid]);

  const checks = useMemo(() => [...(qa?.checks || [])].sort((left, right) => {
    const status = (STATUS_ORDER[left.status] ?? 9) - (STATUS_ORDER[right.status] ?? 9);
    if (status !== 0) return status;
    return (firstEventTime(left) ?? Number.MAX_SAFE_INTEGER) - (firstEventTime(right) ?? Number.MAX_SAFE_INTEGER);
  }), [qa?.checks]);
  const warnings = checks.filter((check) => check.status === "warning");
  const blocked = checks.some((check) => check.status === "blocked" || check.status === "skipped");
  const notCurrent = qa?.currentity === "historical";
  const allWarningReasons = warnings.every((check) => Boolean(warningReasons[check.check_id]?.trim()));
  const mutationBusy = mutationControls.isProjectMutationBusy(projectId);

  if (!qa?.exists) {
    return <section className="delivery-qa-workspace" aria-label="交付 QA">
      <div className="delivery-qa-heading"><div><h3>交付 QA</h3><p>正式輸出成功後會自動建立版本化 QA run；自動檢查不會取代人工觀看成片。</p></div><span className="qa-state needs_qa">等待 QA</span></div>
      <div className="delivery-qa-empty">目前沒有正式輸出的 QA evidence。完成 Render Job 後會在這裡顯示。</div>
    </section>;
  }

  function seek(timestamp: number | null) {
    if (timestamp === null || !videoRef.current) return;
    videoRef.current.currentTime = Math.max(0, timestamp);
    void videoRef.current.play().catch(() => undefined);
  }

  async function submitReview(action: "confirm" | "reject") {
    if (!qa?.qa_run_uuid) return;
    if (action === "confirm" && (!previewConfirmed || blocked || notCurrent || !allWarningReasons)) return;
    if (action === "reject" && !reviewReason.trim()) {
      setMessage("退回交付 QA 前請填寫理由。");
      return;
    }
    const mutation = mutationControls.beginProjectMutation(projectId, "delivery-qa");
    if (!mutation) return;
    setBusy(true);
    try {
      const result = await api.reviewDeliveryQA(projectId, qa.qa_run_uuid, action, qa.human_review.review_version, reviewReason.trim(), warningReasons);
      if (!result.ok) {
        setMessage(`交付 QA 操作失敗：${result.error || "操作未成功"}`);
        return;
      }
      const success = action === "confirm" ? "已完成最終預覽確認，成片可交付" : "已退回交付 QA";
      try {
        await refreshProject({ forceFresh: true });
        setMessage(success);
      } catch (error) {
        setMessage(refreshFailureMessage(success, error));
      }
    } catch (error) {
      setMessage(`交付 QA 操作失敗：${formatApiError(error)}`);
    } finally {
      setBusy(false);
      mutationControls.finishProjectMutation(mutation);
    }
  }

  async function rerun() {
    const mutation = mutationControls.beginProjectMutation(projectId, "delivery-qa");
    if (!mutation) return;
    setBusy(true);
    try {
      const result = await api.rerunDeliveryQA(projectId);
      if (!result.ok) {
        setMessage(`重新檢查失敗：${result.error || "操作未成功"}`);
        return;
      }
      const success = "Delivery QA 已重新檢查；不會重送正式輸出";
      try {
        await refreshProject({ forceFresh: true });
        setMessage(success);
      } catch (error) {
        setMessage(refreshFailureMessage(success, error));
      }
    } catch (error) {
      setMessage(`重新檢查失敗：${formatApiError(error)}`);
    } finally {
      setBusy(false);
      mutationControls.finishProjectMutation(mutation);
    }
  }

  return <section className="delivery-qa-workspace" aria-label="交付 QA">
    <div className="delivery-qa-heading">
      <div><h3>交付 QA</h3><p>Contract {qa.schema_version} · {qa.profile?.profile_id || "general_diary"} · run {runUuid.slice(0, 12)}</p></div>
      <span className={`qa-state ${qa.lifecycle_status}`}>{lifecycleLabel(qa.lifecycle_status)}</span>
    </div>
    {qa.currentity === "historical" && <div className="qa-alert blocked" role="alert">核准快照已變更。這份 QA 僅供歷史稽核，必須重新正式輸出。</div>}
    <div className="qa-counts" aria-label="QA 檢查摘要">
      <span className="pass">通過 <b>{qa.summary.pass}</b></span>
      <span className="warning">警告 <b>{qa.summary.warning}</b></span>
      <span className="blocked">封鎖 <b>{qa.summary.blocked}</b></span>
      <span className="skipped">未驗證 <b>{qa.summary.skipped}</b></span>
    </div>
    {(() => {
      const overview = qa.evidence_index?.find((item) => item.type === "overview_contact_sheet");
      const url = overview ? qa.artifact_urls?.[overview.artifact_id] : undefined;
      return url ? <img className="qa-overview-contact-sheet" src={url} alt="正式輸出 overview contact sheet" /> : null;
    })()}
    {qa.output_url && <div className="qa-preview">
      <video ref={videoRef} controls preload="metadata" src={qa.output_url} aria-label="正式輸出最終預覽" />
      <p>請完整觀看正式輸出。點擊 finding timecode 可跳到對應位置。</p>
    </div>}
    <div className="qa-check-list">
      {checks.map((check) => {
        const timestamp = firstEventTime(check);
        const evidence = (check.evidence_artifact_ids || []).map((id) => ({ id, url: qa.artifact_urls?.[id], meta: qa.evidence_index?.find((item) => item.artifact_id === id) })).filter((item) => item.url);
        return <article className={`qa-check ${check.status}`} key={check.check_id}>
          <header><div><b>{check.check_id}</b><span>{check.summary}</span></div><span className={`qa-check-status ${check.status}`}>{statusLabel(check.status)}</span></header>
          {timestamp !== null && <button type="button" className="qa-timecode" onClick={() => seek(timestamp)}>跳至 {timestamp.toFixed(2)}s</button>}
          <details><summary>Metrics / threshold audit</summary><pre>{JSON.stringify({ metrics: check.metrics, threshold_source: check.threshold_source }, null, 2)}</pre></details>
          {check.remediation && <p className="qa-remediation"><b>建議：</b>{check.remediation}</p>}
          {evidence.length > 0 && <div className="qa-evidence">
            {evidence.map((item) => item.meta?.type === "event_strip" || item.meta?.type?.endsWith("contact_sheet")
              ? <img key={item.id} src={item.url} alt={`${check.check_id} evidence`} />
              : <a key={item.id} href={item.url} target="_blank" rel="noreferrer">開啟 {item.meta?.type || "evidence"}</a>)}
          </div>}
          {check.status === "warning" && <label className="qa-warning-reason">接受此 warning 的理由
            <textarea value={warningReasons[check.check_id] || ""} onChange={(event) => setWarningReasons((current) => ({ ...current, [check.check_id]: event.target.value }))} placeholder="說明為何這是刻意的創作選擇" disabled={busy || qa.deliverable_ready} />
          </label>}
        </article>;
      })}
    </div>
    <div className="qa-human-gate">
      <label>最終預覽備註
        <textarea value={reviewReason} onChange={(event) => setReviewReason(event.target.value)} placeholder="記錄確認或退回理由" disabled={busy || qa.deliverable_ready} />
      </label>
      <label className="qa-preview-confirm"><input type="checkbox" checked={previewConfirmed} onChange={(event) => setPreviewConfirmed(event.target.checked)} disabled={busy || blocked || notCurrent || qa.deliverable_ready} />我已完整觀看正式成片，並確認 warning 接受理由</label>
      {blocked && <p className="qa-alert blocked">blocked 或 skipped 不可直接接受；修正輸出後按「重新檢查」。</p>}
      <div className="row">
        <button type="button" onClick={() => void rerun()} disabled={busy || mutationBusy}>{busy ? "處理中…" : "重新檢查（不重送 Render）"}</button>
        <button type="button" className="danger" onClick={() => void submitReview("reject")} disabled={busy || mutationBusy || qa.deliverable_ready || !reviewReason.trim()}>退回</button>
        <button type="button" className="good" onClick={() => void submitReview("confirm")} disabled={busy || mutationBusy || blocked || notCurrent || qa.deliverable_ready || !previewConfirmed || !allWarningReasons}>確認可交付</button>
      </div>
      {qa.deliverable_ready && <p className="qa-alert ready" role="status">已由本機使用者完成最終預覽確認；此 render fingerprint 可交付。</p>}
    </div>
  </section>;
}
