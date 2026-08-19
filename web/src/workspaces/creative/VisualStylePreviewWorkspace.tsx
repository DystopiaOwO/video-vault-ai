import { useState } from "react";
import { api, formatApiError, type ProjectDetail } from "../../api";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import type { ProjectMutationControls } from "../../projectMutation";
import { VisualStyleDraftProvider, useOptionalVisualStyleDraft, useVisualStyleDraft } from "./VisualStyleDraftController";
import "./visual-style-preview.css";

type Props = {
  detail: ProjectDetail;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<unknown>;
  mutationControls: ProjectMutationControls;
  compact?: boolean;
  advancedSection?: "grading" | "title";
  onApproved?: () => void;
  onPreviewReady?: () => void;
};
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

function VisualStylePreviewWorkspaceInner({ detail, setMessage, refreshProject, mutationControls, compact = false, advancedSection, onApproved, onPreviewReady }: Props) {
  const state = detail.visual_style || {};
  const styles = (state.options?.styles || []).filter((item) => item.enabled_for_round1_ui !== false);
  const primaryStyles = styles.filter((item) => String(item.composition || "overlay") !== "standalone");
  const [showExtraEvidence, setShowExtraEvidence] = useState(false);
  const [busy, setBusy] = useState(false);
  const { draft, setSelectedStyle, selectVariant, setVariants, patchOverride, patchNestedOverride, invalidatePreview } = useVisualStyleDraft();
  const { selectedStyleId: selected, variants, selectedPreviewPlanHash, selectedPreviewVariantId, selectedTitleRole, overrides } = draft;
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

  function variantRole(variant: Record<string, unknown>) { return variant.title_role || mapValue(variant.representative_frame).title_role || ""; }
  function isStaticChapterBright(variant: Record<string, unknown>) {
    const frame = mapValue(variant.representative_frame);
    return String(variant.preview_kind || "static") === "static" && String(variantRole(variant)) === "chapter_title" && String(frame.selection_reason || "") === "bright_high_luma_representative";
  }
  function heroFor(styleId: string) {
    const candidates = variants.filter((item) => String(mapValue(item.visual_style).visual_style_id || "") === styleId);
    return candidates.find(isStaticChapterBright) || candidates.find((item) => String(variantRole(item)) === "chapter_title" && String(item.preview_kind || "static") === "static") || candidates.find((item) => String(item.preview_kind || "static") === "static") || candidates[0];
  }
  function heroForList(source: Array<Record<string, unknown>>, styleId: string) {
    const candidates = source.filter((item) => String(mapValue(item.visual_style).visual_style_id || "") === styleId);
    return candidates.find(isStaticChapterBright) || candidates.find((item) => String(variantRole(item)) === "chapter_title" && String(item.preview_kind || "static") === "static") || candidates.find((item) => String(item.preview_kind || "static") === "static") || candidates[0];
  }
  async function preview() {
    setBusy(true);
    try {
      const result = await api.previewVisualStyles(detail.project.id, false, overrides);
      if (!result.ok) { setMessage(result.error || "Creative Brief 尚未核准，不能產生 authoritative visual preview。"); return; }
      const nextVariants = result.variants || [];
      setVariants(nextVariants);
      const first = heroForList(nextVariants, selected);
      if (first) selectVariant(first, selected);
      onPreviewReady?.();
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

  function advancedControls(section: "grading" | "title") {
    if (section === "grading") {
      return <div className="visual-style-grading-summary">
        <strong>{String(selectedStyle.label || selectedStyle.style_id || "尚未設定")}</strong>
        <p>目前調色與 LUT 由已核准的 Visual Style preset 決定；此處不新增另一套 grading 設定。</p>
        <button type="button" disabled={!approvedBrief || busy} onClick={() => void preview()}>以目前風格重新預覽</button>
      </div>;
    }
    return <div className="visual-style-overrides" aria-label="字卡控制項">
      {!defaultsAvailable && <div className="visual-style-blocked">目前選定的 style/role 沒有 backend resolved control defaults，控制項已停用。</div>}
      <label>字型<select value={String(effective("font_family"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchOverride("font_family", event.target.value)}>{registryOptions(optionData.title_font_families).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>字重<select value={String(effective("weight"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchOverride("weight", Number(event.target.value))}>{registryOptions(optionData.title_weight_values).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>尺寸<select value={String(effective("size_preset"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchOverride("size_preset", event.target.value)}>{registryOptions(optionData.title_size_presets).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>位置<select value={String(effective("anchor"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchOverride("anchor", event.target.value)}>{registryOptions(optionData.title_anchors).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>組成<select value={String(effective("composition"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchOverride("composition", event.target.value)}>{registryOptions(optionData.compositions).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>可讀性<select value={String(effectiveNested("readability", "surface"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchNestedOverride("readability", { surface: event.target.value })}>{registryOptions(optionData.readability_surfaces).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>動畫<select value={String(effectiveNested("motion", "preset"))} disabled={busy || !defaultsAvailable} onChange={(event) => patchNestedOverride("motion", { preset: event.target.value })}>{registryOptions(optionData.title_motion_presets).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <button type="button" disabled={!approvedBrief || busy} onClick={() => void preview()}>以這組設定重新預覽</button>
    </div>;
  }

  if (compact && advancedSection) {
    return <section className="creative-advanced-editor visual-style-advanced-editor" aria-label={advancedSection === "title" ? "字卡詳細設定" : "視覺與調色詳細設定"}>
      <div><strong>{advancedSection === "title" ? "字卡" : "視覺與調色"}</strong><p>{advancedSection === "title" ? "微調字型、位置、可讀性與動畫；變更後需要重新產生預覽。" : "查看目前 preset 的調色與 LUT 摘要；實際 grading contract 仍由 Visual Style registry 提供。"}</p></div>
      {advancedControls(advancedSection)}
    </section>;
  }

  return <section className={`visual-style-preview card${compact ? " visual-style-preview-simple" : ""}`} aria-label="Visual Style Preview">
    <div className="visual-style-heading"><div>{compact ? <><span className="step-kicker">步驟 2／3</span><h3>選一個你喜歡的視覺風格</h3><p>先看真實素材預覽，滿意後再採用；字體與細節可以之後微調。</p></> : <><span className="eyebrow">VISUAL STYLE PREVIEW</span><h3>先用真實畫面確認字幕與視覺方向</h3><p>預覽使用 approved Creative Brief、真實 source frame 與可稽核的 grading/framing contract；不會修改素材。</p></>}</div><span className={approvedBrief ? "brief-status approved" : "brief-status"}>{approvedBrief ? "可產生正式預覽" : "先核准 Creative Brief"}</span></div>
    {!approvedBrief && <div className="visual-style-blocked">目前只顯示 AI 建議：{String((state.recommendation?.label as string) || "Diary Natural")}。Creative Brief 仍是 needs_confirmation，不能自動核准或產生 authoritative Coffee preview。</div>}
    <div className="visual-style-actions"><button type="button" disabled={!approvedBrief || busy} onClick={() => void preview()}>{busy ? "處理中…" : "產生真實畫面預覽"}</button>{approvedBrief && <><select className={compact ? "visual-style-hidden-picker" : ""} aria-label="選擇視覺風格" value={selected} disabled={busy} onChange={(event) => { const next = event.target.value; const match = heroFor(next); if (match) selectVariant(match, next); else { setSelectedStyle(next); invalidatePreview(); } }}>{styles.map((style) => <option key={String(style.style_id)} value={String(style.style_id)}>{String(style.label || style.style_id)}</option>)}</select><button type="button" className="primary" disabled={busy || !selected || !selectedPreviewPlanHash || !selectedPreviewVariantId} onClick={() => void approve()}>{compact ? "使用這個風格並繼續" : "核准選定 Visual Style"}</button></>}</div>
    {approvedBrief && !compact && <div className="visual-style-overrides-shell" aria-label="視覺風格詳細設定">{advancedControls("title")}</div>}
    <div className="visual-style-grid" aria-label="主要視覺風格選擇">{primaryStyles.map((style) => { const styleId = String(style.style_id || ""); const hero = heroFor(styleId); const frame = mapValue(hero?.representative_frame); const isSelected = styleId === selected; const isRecommended = String((state.recommendation as Record<string, unknown> | undefined)?.visual_style_id || "diary_natural") === styleId; return <article className={isSelected ? "selected" : ""} key={styleId} onClick={() => hero ? selectVariant(hero, styleId) : setSelectedStyle(styleId)}><div className="visual-style-preview-media">{hero?.url ? <img src={String(hero.url)} alt={`${String(style.label || styleId)} 主要預覽`} /> : <div className="visual-style-preview-placeholder">產生預覽後顯示</div>}</div><div className="visual-style-card-heading"><h4>{String(style.label || styleId)}</h4>{isRecommended && <span>AI 推薦</span>}</div><p className="visual-style-card-summary">{String(style.description || (String(style.composition || "overlay") === "overlay" ? "自然疊加、保留生活感" : "獨立字卡畫面"))}</p>{hero && <small className="visual-style-hero-meta">{String(frame.selection_reason || "代表畫面")} · 字卡預覽</small>}</article>; })}</div>
    {variants.length > 0 && <div className="visual-style-extra-evidence"><button type="button" onClick={() => setShowExtraEvidence((current) => !current)}>{showExtraEvidence ? "收起其他預覽" : "查看更多預覽"}</button>{showExtraEvidence && <div className="visual-style-extra-grid">{variants.map((variant, index) => { const style = mapValue(variant.visual_style); const frame = mapValue(variant.representative_frame); const animated = String(variant.preview_kind || frame.preview_kind || "static") === "animated"; return <article key={`${String(variant.preview_variant_id || style.visual_style_id)}-${String(variantRole(variant))}-${String(variant.timestamp_seconds || index)}-${String(variant.preview_kind || "static")}`} onClick={() => selectVariant(variant)}><div className="visual-style-preview-media">{animated ? <video src={String(variant.url || "")} controls muted loop playsInline /> : <img src={String(variant.url || "")} alt={`${String(style.label || style.visual_style_id)} ${String(frame.role_label || "補充預覽")}`} />}</div><p>{String(style.label || style.visual_style_id)} · {String(variant.role_label || frame.role_label || "補充預覽")}{animated ? " · 動畫" : ""}</p></article>; })}</div>}</div>}
  </section>;
}

export function VisualStylePreviewWorkspace(props: Props) {
  const existingController = useOptionalVisualStyleDraft();
  if (existingController) return <VisualStylePreviewWorkspaceInner {...props} />;
  return <VisualStyleDraftProvider detail={props.detail}><VisualStylePreviewWorkspaceInner {...props} /></VisualStyleDraftProvider>;
}
