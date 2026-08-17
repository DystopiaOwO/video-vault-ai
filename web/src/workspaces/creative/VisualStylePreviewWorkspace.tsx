import { useState } from "react";
import { api, type ProjectDetail } from "../../api";
import { formatApiError } from "../../api";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import type { ProjectMutationControls } from "../../projectMutation";
import "./visual-style-preview.css";

type Props = { detail: ProjectDetail; setMessage: (value: string) => void; refreshProject: (options?: ProjectDataLoadOptions) => Promise<unknown>; mutationControls: ProjectMutationControls };

export function VisualStylePreviewWorkspace({ detail, setMessage, refreshProject, mutationControls }: Props) {
  const state = detail.visual_style || {};
  const styles = (state.options?.styles || []).filter((item) => item.enabled_for_round1_ui !== false);
  const [variants, setVariants] = useState<Array<Record<string, unknown>>>([]);
  const [selected, setSelected] = useState(String((state.approved?.visual_style_id as string) || "diary_natural"));
  const [selectedPreviewPlanHash, setSelectedPreviewPlanHash] = useState("");
  const [busy, setBusy] = useState(false);
  const approvedBrief = detail.creative_brief?.status === "approved";

  async function preview() {
    setBusy(true);
    try {
      const result = await api.previewVisualStyles(detail.project.id, false);
      if (!result.ok) { setMessage(result.error || "Creative Brief 尚未核准，不能產生 authoritative visual preview。"); return; }
      const nextVariants = result.variants || [];
      setVariants(nextVariants);
      const first = nextVariants.find((item) => String((item.visual_style as Record<string, unknown> | undefined)?.visual_style_id || "") === selected);
      setSelectedPreviewPlanHash(String(first?.preview_plan_hash || ""));
    } catch (error) { setMessage(`Visual Style preview 失敗：${formatApiError(error)}`); }
    finally { setBusy(false); }
  }

  async function approve() {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy(true);
    try {
      const result = await api.approveVisualStyle(detail.project.id, selected, detail.project_revision, selectedPreviewPlanHash);
      if (!result.ok) throw new Error(result.error || "Visual Style 核准失敗");
      await refreshProject({ forceFresh: true });
      setMessage("Visual Style 已保存為 resolved approved snapshot；Render 將使用同一份語意 contract。");
    } catch (error) { setMessage(`Visual Style 核准失敗：${formatApiError(error)}`); }
    finally { mutationControls.finishProjectMutation(mutation); setBusy(false); }
  }

  return <section className="visual-style-preview card" aria-label="Visual Style Preview">
    <div className="visual-style-heading"><div><span className="eyebrow">VISUAL STYLE PREVIEW</span><h3>先用真實畫面確認字幕與視覺方向</h3><p>預覽使用 approved Creative Brief、真實 source frame 與可稽核的 grading/framing contract；不會修改素材。</p></div><span className={approvedBrief ? "brief-status approved" : "brief-status"}>{approvedBrief ? "可產生正式預覽" : "先核准 Creative Brief"}</span></div>
    {!approvedBrief && <div className="visual-style-blocked">目前只顯示 AI 建議：{String((state.recommendation?.label as string) || "Diary Natural")}。Creative Brief 仍是 needs_confirmation，不能自動核准或產生 authoritative Coffee preview。</div>}
    <div className="visual-style-actions"><button type="button" disabled={!approvedBrief || busy} onClick={() => void preview()}>{busy ? "處理中…" : "產生真實畫面預覽"}</button>{approvedBrief && <><select value={selected} disabled={busy} onChange={(event) => { setSelected(event.target.value); const match = variants.find((item) => String((item.visual_style as Record<string, unknown> | undefined)?.visual_style_id || "") === event.target.value); setSelectedPreviewPlanHash(String(match?.preview_plan_hash || "")); }}>{styles.map((style) => <option key={String(style.style_id)} value={String(style.style_id)}>{String(style.label || style.style_id)}</option>)}</select><button type="button" className="primary" disabled={busy || !selected || !selectedPreviewPlanHash} onClick={() => void approve()}>核准選定 Visual Style</button></>}</div>
    {variants.length > 0 && <div className="visual-style-grid">{variants.map((variant, index) => { const style = (variant.visual_style || {}) as Record<string, unknown>; const source = (variant.source || {}) as Record<string, unknown>; const frame = (variant.representative_frame || {}) as Record<string, unknown>; return <article key={`${String(style.visual_style_id)}-${String(frame.selection_reason || "frame")}-${String(variant.timestamp_seconds || index)}`} onClick={() => { setSelected(String(style.visual_style_id || selected)); setSelectedPreviewPlanHash(String(variant.preview_plan_hash || "")); }}><img src={String(variant.url || "")} alt={`${String(style.label || style.visual_style_id)} ${String(frame.selection_reason || "representative")} frame preview`} /><h4>{String(style.label || style.visual_style_id)}</h4><p>{String(style.composition || "overlay")} · grading {String((style.grading as Record<string, unknown>)?.look_id || "unknown")} · source {String(source.project_media_uuid || "")} · {String(frame.selection_reason || "representative")}</p><code>{String(variant.preview_plan_hash || style.resolved_hash || "").slice(0, 16)}</code></article>; })}</div>}
  </section>;
}
