import { useState } from "react";
import { api, type CloudReviewPlan } from "../../api";

type Props = {
  projectId: number;
  projectRevision?: number;
  disabled?: boolean;
  setMessage: (message: string) => void;
  refreshProject: (options?: { forceFresh?: boolean; throwOnError?: boolean }) => Promise<unknown>;
};

function planLabel(plan: CloudReviewPlan | null): string {
  if (!plan) return "尚未建立複判預覽";
  if (plan.status === "disabled") return "Cloud review 尚未啟用";
  if (plan.status === "no_eligible_windows") return "目前沒有符合條件的區段";
  if (plan.status === "budget_exceeded") return "已超過複判預算上限";
  return `預計上傳 ${plan.estimated_frames} 張抽幀／${plan.estimated_calls} 次呼叫`;
}

export function CloudReviewWorkspace({ projectId, projectRevision, disabled, setMessage, refreshProject }: Props) {
  const [plan, setPlan] = useState<CloudReviewPlan | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);

  async function preview() {
    setLoading(true);
    try {
      const result = await api.cloudReviewPlan(projectId);
      if (!result.ok || !result.plan) {
        setMessage(`雲端複判預覽失敗：${result.error || "無法建立預覽"}`);
        return;
      }
      setPlan(result.plan);
      setSelected(result.plan.windows.map((item) => item.window_uuid));
      setMessage(planLabel(result.plan));
    } catch (error) {
      setMessage(`雲端複判預覽失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally {
      setLoading(false);
    }
  }

  async function send() {
    if (!plan || !selected.length) return;
    setSending(true);
    try {
      const result = await api.cloudReview(projectId, projectRevision, selected);
      if (!result.ok) {
        setMessage(`雲端複判未完成，已保留本地結果：${result.error || "provider unavailable"}`);
      } else {
        setMessage(`雲端複判完成：${selected.length} 個區段；仍需人工確認。`);
      }
      await refreshProject({ forceFresh: true, throwOnError: true });
    } catch (error) {
      setMessage(`雲端複判未完成，已保留本地結果：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally {
      setSending(false);
    }
  }

  return <section className="cloud-review-workspace" aria-label="低信心雲端複判">
    <div className="clip-summary-heading">
      <strong>低信心雲端複判</strong>
      <span>{planLabel(plan)}</span>
    </div>
    <p className="muted">只會送出明確選定的抽幀，不會上傳整支影片；provider、影格數與估算成本會在送出前顯示。</p>
    <div className="row">
      <button type="button" disabled={disabled || loading || sending} onClick={() => void preview()}>{loading ? "檢查中…" : "檢查可複判區段"}</button>
      {plan && <span>Provider：{plan.provider} · 估算成本：${plan.estimated_cost_usd.toFixed(4)} USD</span>}
    </div>
    {plan?.windows.map((item) => <label key={item.window_uuid} className="cloud-review-window">
      <input
        type="checkbox"
        checked={selected.includes(item.window_uuid)}
        disabled={disabled || sending}
        onChange={(event) => setSelected((current) => event.target.checked ? [...current, item.window_uuid] : current.filter((value) => value !== item.window_uuid))}
      />
      <span>視窗 {item.ordinal || "-"} · {item.frame_count} 張 · 信心 {Math.round(item.confidence * 100)}% · {item.reasons.join("、")}</span>
    </label>)}
    {!!plan?.rejected_windows.length && <small className="muted">另有 {plan.rejected_windows.length} 個區段因 clip/project 上限未加入預覽。</small>}
    {plan?.status === "ready" && <button type="button" className="good" disabled={disabled || sending || !selected.length} onClick={() => void send()}>{sending ? "送出複判中…" : `送出 ${selected.length} 個區段`}</button>}
  </section>;
}
