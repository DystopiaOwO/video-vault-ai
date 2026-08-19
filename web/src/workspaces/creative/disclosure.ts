import type { EditorDisclosureMetadata, EditorDisclosureSection, ProjectDetail } from "../../api";

const FALLBACK_DISCLOSURE: EditorDisclosureSection[] = [
  { section_id: "output_direction", version: "1", label: "影片方向", disclosure_level: "primary", order: 10, summary_resolver: "creative_brief.output@1", semantic_domain: "creative_brief.visual", invalidation_class: "visual_render_only", include_in_final_summary: true, summary_order: 10 },
  { section_id: "visual_style", version: "1", label: "視覺風格", disclosure_level: "primary", order: 20, summary_resolver: "visual_style.approved@1", semantic_domain: "visual_style", invalidation_class: "visual_preview_and_render", include_in_final_summary: true, summary_order: 20 },
  { section_id: "framing", version: "1", label: "畫面配置", disclosure_level: "advanced", order: 30, summary_resolver: "creative_brief.framing@1", semantic_domain: "creative_brief.visual", invalidation_class: "visual_render_only", action: { type: "open_semantic_editor@1", target: "creative_brief.framing" }, include_in_final_summary: true, summary_order: 30 },
  { section_id: "grading", version: "1", label: "視覺與調色", disclosure_level: "advanced", order: 40, summary_resolver: "visual_style.grading@1", semantic_domain: "visual_style", invalidation_class: "visual_preview_and_render", action: { type: "open_semantic_editor@1", target: "visual_style.grading" }, summary_order: 40 },
  { section_id: "title", version: "1", label: "字卡", disclosure_level: "advanced", order: 50, summary_resolver: "visual_style.title_style@1", semantic_domain: "visual_style.title", invalidation_class: "visual_preview_and_render", action: { type: "open_semantic_editor@1", target: "visual_style.title_style" }, include_in_final_summary: true, summary_order: 50 },
  { section_id: "captions", version: "1", label: "字幕", disclosure_level: "advanced", order: 60, summary_resolver: "creative_brief.captions@1", semantic_domain: "caption_policy", invalidation_class: "caption_renderer", capability: { status: "summary_only", owner: "future_subtitle_task" }, action: { type: "open_semantic_editor@1", target: "caption.summary" }, summary_order: 60 },
  { section_id: "technical", version: "1", label: "技術資訊", disclosure_level: "diagnostic", order: 90, summary_resolver: "diagnostic.contracts@1", semantic_domain: "diagnostic", invalidation_class: "none", summary_order: 90 },
];

export function disclosureSections(detail: ProjectDetail, level?: string): EditorDisclosureSection[] {
  const metadata: EditorDisclosureMetadata | undefined = detail.editor_disclosure;
  const sections = metadata?.sections?.length ? metadata.sections : FALLBACK_DISCLOSURE;
  return sections
    .filter((section) => !level || section.disclosure_level === level)
    .sort((left, right) => left.order - right.order || left.section_id.localeCompare(right.section_id));
}

export function disclosureSection(detail: ProjectDetail, id: string): EditorDisclosureSection | undefined {
  return disclosureSections(detail).find((section) => section.section_id === id);
}

export const disclosureFallback = FALLBACK_DISCLOSURE;

export type DisclosureSummary = { text: string; available: boolean };
export type DisclosureAction = { available: boolean; label: string; step?: "direction" | "style"; reason?: string };

type SummaryResolver = (detail: ProjectDetail) => string;

function outputSummary(detail: ProjectDetail): string {
  const brief = detail.creative_brief || {};
  const output = brief.approved?.output || brief.recommendation?.output || {};
  const option = (brief.options?.output_contracts || []).find((item) => String(item.output_contract_id) === String(output.output_contract_id));
  return `${String(option?.label || output.aspect_ratio || "尚未設定")} · ${String(output.width || "—")}×${String(output.height || "—")}`;
}

function styleObject(detail: ProjectDetail): Record<string, unknown> {
  return (detail.visual_style?.approved || detail.visual_style?.recommendation || {}) as Record<string, unknown>;
}

function styleLabel(detail: ProjectDetail): string {
  const style = styleObject(detail);
  const id = String(style.visual_style_id || "");
  const option = (detail.visual_style?.options?.styles || []).find((item) => String(item.style_id) === id);
  return String(option?.label || style.label || id || "尚未設定");
}

function framingSummary(detail: ProjectDetail): string {
  const brief = detail.creative_brief || {};
  const framing = brief.approved?.framing_intent || brief.recommendation?.framing_intent || {};
  const strategies = Object.values(framing).map((item) => {
    const value = item as Record<string, unknown>;
    const id = value.approved_strategy_id || value.approved_strategy || value.recommended_strategy_id || value.recommended_strategy;
    const option = (brief.options?.framing_strategies || []).find((candidate) => String(candidate.strategy_id) === String(id));
    return String(option?.label || id || "");
  }).filter(Boolean);
  return strategies.length ? strategies.join("；") : "依素材自動處理畫面";
}

const SUMMARY_RESOLVERS: Record<string, SummaryResolver> = {
  "creative_brief.output@1": outputSummary,
  "creative_brief.framing@1": framingSummary,
  "visual_style.approved@1": (detail) => {
    const style = styleObject(detail);
    const composition = String(style.composition || "overlay");
    return `${styleLabel(detail)} · ${composition === "standalone" ? "獨立字卡畫面" : "疊在影片上"}`;
  },
  "visual_style.grading@1": (detail) => `跟隨${styleLabel(detail)}的調色`,
  "visual_style.title_style@1": (detail) => {
    const style = styleObject(detail);
    const id = String(style.title_style_id || "");
    const option = (detail.visual_style?.options?.title_styles || []).find((item) => String(item.title_style_id) === id);
    return String(option?.label || id || "預設字卡");
  },
  "creative_brief.captions@1": () => "字幕樣式可於字幕功能中調整",
  "diagnostic.contracts@1": (detail) => `目前使用 ${detail.editor_disclosure?.registry_version || "editor-disclosure registry"}`,
  "audio.policy@1": () => "使用專案音訊偏好（測試）",
};

const ACTION_RESOLVERS: Record<string, (target: string) => DisclosureAction> = {
  "open_semantic_editor@1": (target) => {
    const step = target === "creative_brief.framing" ? "direction" : target.startsWith("visual_style.") ? "style" : undefined;
    return step ? { available: true, label: "微調", step } : { available: false, label: "目前不可用", reason: "此設定尚未提供可開啟的編輯入口" };
  },
};

export function resolveDisclosureSummary(section: EditorDisclosureSection, detail: ProjectDetail): DisclosureSummary {
  const resolver = SUMMARY_RESOLVERS[section.summary_resolver];
  if (!resolver) return { text: "此區塊摘要暫不可用（未註冊的 resolver）", available: false };
  if (section.enabled === false) return { text: "此功能目前不可用", available: false };
  return { text: resolver(detail), available: true };
}

export function resolveDisclosureAction(section: EditorDisclosureSection): DisclosureAction {
  const action = section.action;
  if (!action || action.type === undefined || action.target === undefined) {
    return { available: false, label: "僅供摘要", reason: "此區塊目前沒有可開啟的編輯入口" };
  }
  const resolver = ACTION_RESOLVERS[String(action.type)];
  if (!resolver) return { available: false, label: "目前不可用", reason: "此區塊的 action resolver 尚未註冊" };
  return resolver(String(action.target));
}
