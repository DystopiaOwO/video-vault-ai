import type { EditorDisclosureMetadata, EditorDisclosureSection, ProjectDetail } from "../../api";

const FALLBACK_DISCLOSURE: EditorDisclosureSection[] = [
  { section_id: "output_direction", version: "1", label: "影片方向", disclosure_level: "primary", order: 10, summary_resolver: "creative_brief.output", semantic_domain: "creative_brief.visual", invalidation_class: "visual_render_only" },
  { section_id: "visual_style", version: "1", label: "視覺風格", disclosure_level: "primary", order: 20, summary_resolver: "visual_style.approved", semantic_domain: "visual_style", invalidation_class: "visual_preview_and_render" },
  { section_id: "framing", version: "1", label: "畫面配置", disclosure_level: "advanced", order: 30, summary_resolver: "creative_brief.framing", semantic_domain: "creative_brief.visual", invalidation_class: "visual_render_only" },
  { section_id: "grading", version: "1", label: "視覺與調色", disclosure_level: "advanced", order: 40, summary_resolver: "visual_style.grading", semantic_domain: "visual_style", invalidation_class: "visual_preview_and_render" },
  { section_id: "title", version: "1", label: "字卡", disclosure_level: "advanced", order: 50, summary_resolver: "visual_style.title_style", semantic_domain: "visual_style.title", invalidation_class: "visual_preview_and_render" },
  { section_id: "captions", version: "1", label: "字幕", disclosure_level: "advanced", order: 60, summary_resolver: "creative_brief.captions", semantic_domain: "caption_policy", invalidation_class: "caption_renderer", capability: { status: "summary_only", owner: "future_subtitle_task" } },
  { section_id: "technical", version: "1", label: "技術資訊", disclosure_level: "diagnostic", order: 90, summary_resolver: "diagnostic.contracts", semantic_domain: "diagnostic", invalidation_class: "none" },
];

export function disclosureSections(detail: ProjectDetail, level?: string): EditorDisclosureSection[] {
  const metadata: EditorDisclosureMetadata | undefined = detail.editor_disclosure;
  const sections = metadata?.sections?.length ? metadata.sections : FALLBACK_DISCLOSURE;
  return sections
    .filter((section) => section.enabled !== false && (!level || section.disclosure_level === level))
    .sort((left, right) => left.order - right.order || left.section_id.localeCompare(right.section_id));
}

export function disclosureSection(detail: ProjectDetail, id: string): EditorDisclosureSection | undefined {
  return disclosureSections(detail).find((section) => section.section_id === id);
}

export const disclosureFallback = FALLBACK_DISCLOSURE;
