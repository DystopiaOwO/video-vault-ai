import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { ProjectDetail } from "../../api";

export type VisualStyleDraft = {
  selectedStyleId: string;
  overrides: Record<string, unknown>;
  variants: Array<Record<string, unknown>>;
  selectedPreviewPlanHash: string;
  selectedPreviewVariantId: string;
  selectedTitleRole: string;
};

export type VisualStyleDraftController = {
  draft: VisualStyleDraft;
  setSelectedStyle: (styleId: string) => void;
  selectVariant: (variant: Record<string, unknown>, styleId?: string) => void;
  setVariants: (variants: Array<Record<string, unknown>>) => void;
  patchOverride: (key: string, value: unknown) => void;
  patchNestedOverride: (key: string, value: Record<string, unknown>) => void;
  invalidatePreview: () => void;
};

const VisualStyleDraftContext = createContext<VisualStyleDraftController | null>(null);

function mapValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function approvedOutput(detail: ProjectDetail): Record<string, unknown> {
  return mapValue(mapValue(detail.creative_brief?.approved).output);
}

function styleControlDefaults(detail: ProjectDetail, styleId: string, role = "chapter_title"): Record<string, unknown> {
  const options = mapValue(detail.visual_style?.options);
  const output = approvedOutput(detail);
  const aspect = String(output.orientation || "landscape");
  const defaults = Array.isArray(options.control_defaults) ? options.control_defaults : [];
  return defaults
    .map(mapValue)
    .find((item) => String(item.visual_style_id || "") === styleId && item.is_default_title_style === true && String(item.role || "chapter_title") === role && String(item.aspect || aspect) === aspect) || {};
}

function legalOverridesForStyle(detail: ProjectDetail, styleId: string, overrides: Record<string, unknown>, role = "chapter_title"): Record<string, unknown> {
  const defaults = styleControlDefaults(detail, styleId, role);
  if (!Object.keys(defaults).length) return {};
  const result: Record<string, unknown> = {};
  const scalarKeys = ["title_style_id", "title_style_version", "font_family", "weight", "size_preset", "anchor", "composition", "palette_variant"];
  for (const key of scalarKeys) {
    if (key in overrides && key in defaults) result[key] = overrides[key];
  }
  for (const key of ["readability", "motion", "palette"]) {
    const override = mapValue(overrides[key]);
    const defaultValue = mapValue(defaults[key]);
    const nested = Object.fromEntries(Object.entries(override).filter(([nestedKey]) => nestedKey in defaultValue));
    if (Object.keys(nested).length) result[key] = nested;
  }
  return result;
}

function initialDraft(detail: ProjectDetail): VisualStyleDraft {
  const visualStyle = detail.visual_style || {};
  const approved = mapValue(visualStyle.approved);
  const recommendation = mapValue(visualStyle.recommendation);
  const persisted = Object.keys(approved).length ? approved : recommendation;
  const selectedStyleId = String(persisted.visual_style_id || "diary_natural");
  const title = mapValue(persisted.title_style);
  const selectedTitleRole = String(title.role || persisted.title_role || "chapter_title");
  const persistedOverrides = legalOverridesForStyle(detail, selectedStyleId, mapValue(persisted.overrides), selectedTitleRole);
  return {
    selectedStyleId,
    overrides: persistedOverrides,
    variants: [],
    selectedPreviewPlanHash: String(persisted.approved_preview_plan_hash || persisted.approval_envelope && mapValue(persisted.approval_envelope).preview_plan_hash || ""),
    selectedPreviewVariantId: String(persisted.approved_preview_variant_id || persisted.approval_envelope && mapValue(persisted.approval_envelope).preview_variant_id || ""),
    selectedTitleRole,
  };
}

function sourceIdentity(detail: ProjectDetail): string {
  const visualStyle = detail.visual_style || {};
  const approved = mapValue(visualStyle.approved);
  const recommendation = mapValue(visualStyle.recommendation);
  const options = mapValue(visualStyle.options);
  return JSON.stringify({
    projectRevision: detail.project_revision,
    status: visualStyle.status,
    approved,
    recommendation,
    registryHash: options.registry_hash,
  });
}

export function VisualStyleDraftProvider({ detail, children }: { detail: ProjectDetail; children: ReactNode }) {
  const identity = sourceIdentity(detail);
  const [draft, setDraft] = useState<VisualStyleDraft>(() => initialDraft(detail));
  const [materializedIdentity, setMaterializedIdentity] = useState(identity);

  useEffect(() => {
    if (identity === materializedIdentity) return;
    setDraft(initialDraft(detail));
    setMaterializedIdentity(identity);
  }, [detail, identity, materializedIdentity]);

  const controller = useMemo<VisualStyleDraftController>(() => ({
    draft,
    setSelectedStyle: (styleId) => setDraft((current) => current.selectedStyleId === styleId ? current : {
      ...current,
      selectedStyleId: styleId,
      overrides: legalOverridesForStyle(detail, styleId, current.overrides, current.selectedTitleRole),
      selectedPreviewPlanHash: "",
      selectedPreviewVariantId: "",
    }),
    selectVariant: (variant, styleId) => {
      const visualStyle = mapValue(variant.visual_style);
      const representative = mapValue(variant.representative_frame);
      setDraft((current) => ({
        ...current,
        selectedStyleId: String(styleId || visualStyle.visual_style_id || current.selectedStyleId),
        overrides: legalOverridesForStyle(detail, String(styleId || visualStyle.visual_style_id || current.selectedStyleId), current.overrides, String(variant.title_role || representative.title_role || current.selectedTitleRole || "chapter_title")),
        selectedPreviewPlanHash: String(variant.preview_plan_hash || ""),
        selectedPreviewVariantId: String(variant.preview_variant_id || ""),
        selectedTitleRole: String(variant.title_role || representative.title_role || "chapter_title"),
      }));
    },
    setVariants: (variants) => setDraft((current) => ({ ...current, variants })),
    patchOverride: (key, value) => setDraft((current) => ({
      ...current,
      overrides: { ...current.overrides, [key]: value },
      selectedPreviewPlanHash: "",
      selectedPreviewVariantId: "",
    })),
    patchNestedOverride: (key, value) => setDraft((current) => ({
      ...current,
      overrides: { ...current.overrides, [key]: { ...mapValue(current.overrides[key]), ...value } },
      selectedPreviewPlanHash: "",
      selectedPreviewVariantId: "",
    })),
    invalidatePreview: () => setDraft((current) => ({ ...current, selectedPreviewPlanHash: "", selectedPreviewVariantId: "" })),
  }), [detail, draft]);

  return <VisualStyleDraftContext.Provider value={controller}>{children}</VisualStyleDraftContext.Provider>;
}

export function useOptionalVisualStyleDraft(): VisualStyleDraftController | null {
  return useContext(VisualStyleDraftContext);
}

export function useVisualStyleDraft(): VisualStyleDraftController {
  const controller = useOptionalVisualStyleDraft();
  if (!controller) throw new Error("VisualStylePreviewWorkspace requires a VisualStyleDraftProvider");
  return controller;
}
