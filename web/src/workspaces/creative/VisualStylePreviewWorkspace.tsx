import { useState } from "react";
import { api, type ProjectDetail } from "../../api";
import { formatApiError } from "../../api";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import type { ProjectMutationControls } from "../../projectMutation";
import "./visual-style-preview.css";

type Props = { detail: ProjectDetail; setMessage: (value: string) => void; refreshProject: (options?: ProjectDataLoadOptions) => Promise<unknown>; mutationControls: ProjectMutationControls; compact?: boolean; onApproved?: () => void };
type RegistryOption = { id: string; label: string; enabled?: boolean };

function registryOptions(value: unknown): RegistryOption[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const option = (item || {}) as Record<string, unknown>;
    return { id: String(option.id ?? ""), label: String(option.label ?? option.id ?? ""), enabled: option.enabled !== false };
  }).filter((item) => item.id && item.enabled !== false);
}

function mapValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

export function VisualStylePreviewWorkspace({ detail, setMessage, refreshProject, mutationControls, compact = false, onApproved }: Props) {
  const state = detail.visual_style || {};
  const styles = (state.options?.styles || []).filter((item) => item.enabled_for_round1_ui !== false);
  const [variants, setVariants] = useState<Array<Record<string, unknown>>>([]);
  const [selected, setSelected] = useState(String((state.approved?.visual_style_id as string) || "diary_natural"));
  const [selectedPreviewPlanHash, setSelectedPreviewPlanHash] = useState("");
  const [selectedPreviewVariantId, setSelectedPreviewVariantId] = useState("");
  const [selectedTitleRole, setSelectedTitleRole] = useState("chapter_title");
  const [overrides, setOverrides] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState(false);
  const approvedBrief = detail.creative_brief?.status === "approved";
  const optionData = (state.options || {}) as Record<string, unknown>;
  const selectedStyle = styles.find((item) => String(item.style_id || "") === selected) || styles[0] || {};
  const approvedOutput = mapValue(mapValue(detail.creative_brief?.approved).output);
  const aspect = String(approvedOutput.orientation || "landscape");
  const controlDefaults = (Array.isArray(optionData.control_defaults) ? optionData.control_defaults : []) as Array<Record<string, unknown>>;
  const resolvedDefaults = controlDefaults.find((item) => item.visual_style_id === selectedStyle.style_id && item.is_default_title_style === true && item.role === selectedTitleRole && item.aspect === aspect);
  const defaultsAvailable = Boolean(resolvedDefaults);
  const effective = (key: string) => overrides[key] ?? resolvedDefaults?.[key] ?? "";
  const effectiveNested = (container: string, key: string) => mapValue(overrides[container])[key] ?? mapValue(resolvedDefaults?.[container])[key] ?? "";

  function invalidatePreview() { setSelectedPreviewPlanHash(""); setSelectedPreviewVariantId(""); }
  function patchOverride(key: string, value: unknown) { setOverrides((old) => ({ ...old, [key]: value })); invalidatePreview(); }
  function patchNestedOverride(key: string, value: Record<string, unknown>) { setOverrides((old) => ({ ...old, [key]: { ...(old[key] as Record<string, unknown> || {}), ...value } })); invalidatePreview(); }

  async function preview() {
    setBusy(true);
    try {
      const result = await api.previewVisualStyles(detail.project.id, false, overrides);
      if (!result.ok) { setMessage(result.error || "Creative Brief 尚未核准，不能產生 authoritative visual preview。"); return; }
      const nextVariants = result.variants || [];
      setVariants(nextVariants);
      const first = nextVariants.find((item) => String((item.visual_style as Record<string, unknown> | undefined)?.visual_style_id || "") === selected);
      setSelectedPreviewPlanHash(String(first?.preview_plan_hash || ""));
      setSelectedPreviewVariantId(String(first?.preview_variant_id || ""));
      setSelectedTitleRole(String(first?.title_role || "chapter_title"));
    } catch (error) { setMessage(`Visual Style preview 失敗：${formatApiError(error)}`); }
    finally { setBusy(false); }
  }

  async function approve() {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy(true);
    try {
      const result = await api.approveVisualStyle(detail.project.id, selected, detail.project_revision, selectedPreviewPlanHash, selectedPreviewVariantId, selectedTitleRole, overrides);
      if (!result.ok) throw new Error(result.error || "Visual Style 核准失敗");
      await refreshProject({ forceFresh: true });
      setMessage("Visual Style 已保存為 resolved approved snapshot；Render 將使用同一份語意 contract。");
      onApproved?.();
    } catch (error) { setMessage(`Visual Style 核准失敗：${formatApiError(error)}`); }
    finally { mutationControls.finishProjectMutation(mutation); setBusy(false); }
  }

  return <section className={`visual-style-preview card${compact ? " visual-style-preview-simple" : ""}`} aria-label="Visual Style Preview">
    <div className="visual-style-heading"><div>{compact ? <><span className="step-kicker">步驟 2／3</span><h3>選一個你喜歡的視覺風格</h3><p>先看真實素材預覽，滿意後再採用；字體與細節可以之後微調。</p></> : <><span className="eyebrow">VISUAL STYLE PREVIEW</span><h3>先用真實畫面確認字幕與視覺方向</h3><p>預覽使用 approved Creative Brief、真實 source frame 與可稽核的 grading/framing contract；不會修改素材。</p></>}</div><span className={approvedBrief ? "brief-status approved" : "brief-status"}>{approvedBrief ? "可產生正式預覽" : "先核准 Creative Brief"}</span></div>
    {!approvedBrief && <div className="visual-style-blocked">目前只顯示 AI 建議：{String((state.recommendation?.label as string) || "Diary Natural")}。Creative Brief 仍是 needs_confirmation，不能自動核准或產生 authoritative Coffee preview。</div>}
    <div className="visual-style-actions"><button type="button" disabled={!approvedBrief || busy} onClick={() => void preview()}>{busy ? "處理中…" : "產生真實畫面預覽"}</button>{approvedBrief && <><select className={compact ? "visual-style-hidden-picker" : ""} aria-label="選擇視覺風格" value={selected} disabled={busy} onChange={(event) => { setSelected(event.target.value); const match = variants.find((item) => String((item.visual_style as Record<string, unknown> | undefined)?.visual_style_id || "") === event.target.value); setSelectedPreviewPlanHash(String(match?.preview_plan_hash || "")); setSelectedPreviewVariantId(String(match?.preview_variant_id || "")); setSelectedTitleRole(String(match?.title_role || "chapter_title")); }}>{styles.map((style) => <option key={String(style.style_id)} value={String(style.style_id)}>{String(style.label || style.style_id)}</option>)}</select><button type="button" className="primary" disabled={busy || !selected || !selectedPreviewPlanHash || !selectedPreviewVariantId} onClick={() => void approve()}>{compact ? "使用這個風格並繼續" : "核准選定 Visual Style"}</button></>}</div>
    {approvedBrief && <details className={compact ? "visual-style-advanced" : "visual-style-advanced open"} open={!compact} aria-label="視覺風格詳細設定">
      {compact && <summary>詳細設定</summary>}
      <div className="visual-style-overrides" aria-label="bounded visual style overrides">
      {!defaultsAvailable && <div className="visual-style-blocked">目前選定的 style/role 沒有 backend resolved control defaults，控制項已停用。</div>}
      <label>字型<select value={String(effective("font_family"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchOverride("font_family", event.target.value)}>{registryOptions(optionData.title_font_families).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>字重<select value={String(effective("weight"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchOverride("weight", Number(event.target.value))}>{registryOptions(optionData.title_weight_values).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>尺寸<select value={String(effective("size_preset"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchOverride("size_preset", event.target.value)}>{registryOptions(optionData.title_size_presets).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>位置<select value={String(effective("anchor"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchOverride("anchor", event.target.value)}>{registryOptions(optionData.title_anchors).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>組成<select value={String(effective("composition"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchOverride("composition", event.target.value)}>{registryOptions(optionData.compositions).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>可讀性<select value={String(effectiveNested("readability", "surface"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchNestedOverride("readability", { surface: event.target.value })}>{registryOptions(optionData.readability_surfaces).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>動畫<select value={String(effectiveNested("motion", "preset"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchNestedOverride("motion", { preset: event.target.value })}>{registryOptions(optionData.title_motion_presets).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <button type="button" disabled={!approvedBrief || busy} onClick={() => void preview()}>以這組設定重新預覽</button>
      </div>
    </details>}
    {variants.length > 0 && <div className="visual-style-grid">{variants.map((variant, index) => { const style = (variant.visual_style || {}) as Record<string, unknown>; const source = (variant.source || {}) as Record<string, unknown>; const frame = (variant.representative_frame || {}) as Record<string, unknown>; const animated = String(variant.preview_kind || frame.preview_kind || "static") === "animated"; const isSelected = String(style.visual_style_id || "") === selected; const isRecommended = String((state.recommendation as Record<string, unknown> | undefined)?.visual_style_id || "diary_natural") === String(style.visual_style_id || ""); return <article className={isSelected ? "selected" : ""} key={`${String(variant.preview_variant_id || style.visual_style_id)}-${String(frame.title_role || "chapter_title")}-${String(variant.timestamp_seconds || index)}-${String(variant.preview_kind || "static")}`} onClick={() => { setSelected(String(style.visual_style_id || selected)); setSelectedPreviewPlanHash(String(variant.preview_plan_hash || "")); setSelectedPreviewVariantId(String(variant.preview_variant_id || "")); setSelectedTitleRole(String(variant.title_role || frame.title_role || "chapter_title")); }}><div className="visual-style-preview-media">{animated ? <video src={String(variant.url || "")} controls muted loop playsInline /> : <img src={String(variant.url || "")} alt={`${String(style.label || style.visual_style_id)} ${String(frame.role_label || frame.selection_reason || "representative")} frame preview`} />}</div><div className="visual-style-card-heading"><h4>{String(style.label || style.visual_style_id)}</h4>{isRecommended && <span>AI 推薦</span>}</div><p className={compact ? "visual-style-card-summary" : ""}>{compact ? String(style.description || (style.composition === "overlay" ? "自然疊加、保留生活感" : "具有明顯風格的畫面處理")) : `${String(variant.role_label || frame.role_label || "Chapter")}${animated ? " · 動畫" : ""}`}</p>{!compact && <><p>{String(style.composition || "overlay")} · grading {String((style.grading as Record<string, unknown>)?.look_id || "unknown")} · source {String(source.project_media_uuid || "")} · {String(frame.selection_reason || "representative")}</p><code>{String(variant.preview_plan_hash || style.semantic_hash || style.resolved_hash || "").slice(0, 16)}</code></>}</article>; })}</div>}
  </section>;
}
