import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  formatApiError,
  type ColorAdjustment,
  type ColorSegmentState,
  type ColorState,
  type ColorStatePatch,
  type Job,
  type ProjectDetail,
  type Segment,
} from "../../api";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import { createProjectMutationControls, mutationLabel, ProjectMutationCoordinator, type ProjectMutationControls } from "../../projectMutation";
import "./color-consistency-workspace.css";

export type ColorConsistencyWorkspaceProps = {
  detail: ProjectDetail;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]>;
  mutationControls?: ProjectMutationControls;
};

type ColorPreviewItem = {
  video_id: number;
  segment_id: string;
  before_url: string;
  after_url: string;
  cache_hit: boolean;
  confidence?: number;
  warnings?: string[];
};

type BusyAction = "" | "analyze" | "reference" | "save" | "preview";

const EMPTY_ADJUSTMENT: ColorAdjustment = {
  mode: "none",
  lut_path: "",
  lut_kind: "",
  exposure: 0,
  temperature: 0,
  tint: 0,
  contrast: 1,
  saturation: 1,
  gamma: 1,
  highlights: 0,
  shadows: 0,
};

export function ColorConsistencyWorkspace({ detail, setMessage, refreshProject, mutationControls }: ColorConsistencyWorkspaceProps) {
  const initial = cloneColorState(detail.color || emptyColorState());
  const [state, setState] = useState<ColorState>(initial);
  const [baseline, setBaseline] = useState<ColorState>(initial);
  const [previews, setPreviews] = useState<ColorPreviewItem[]>([]);
  const [segmentQuery, setSegmentQuery] = useState("");
  const [busy, setBusy] = useState<BusyAction>("");
  const projectIdRef = useRef(detail.project.id);
  const dirty = useMemo(() => colorSignature(state) !== colorSignature(baseline), [baseline, state]);
  const dirtyRef = useRef(dirty);
  const fallbackControlsRef = useRef<ProjectMutationControls | null>(null);
  if (!fallbackControlsRef.current) fallbackControlsRef.current = createProjectMutationControls(new ProjectMutationCoordinator());
  const controls = mutationControls || fallbackControlsRef.current;
  const projectMutationBusy = controls.isProjectMutationBusy(detail.project.id);
  const workspaceBusy = Boolean(busy) || projectMutationBusy;

  function setProjectMessage(message: string) {
    if (controls.isCurrentProject(detail.project.id)) setMessage(message);
  }

  function requireProjectRevision(): number | null {
    if (typeof detail.project_revision === "number") return detail.project_revision;
    setProjectMessage("專案版本資料遺失，請重新載入後再進行調色操作。");
    return null;
  }
  const selectedReferenceId = isColorReference(state.reference) ? state.reference.id : "";
  const requiresLutPath = state.enabled && state.applied.mode === "dji_lut" && !state.applied.lut_path.trim();

  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => {
    if (!dirty) return;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [dirty]);

  useEffect(() => {
    const incoming = cloneColorState(detail.color || emptyColorState());
    const projectChanged = projectIdRef.current !== detail.project.id;
    if (projectChanged) {
      projectIdRef.current = detail.project.id;
      setState(incoming);
      setBaseline(incoming);
      setPreviews([]);
      setSegmentQuery("");
      setBusy("");
      return;
    }
    if (!dirtyRef.current) {
      setState(incoming);
      setBaseline(incoming);
    }
  }, [detail.color, detail.project.id]);

  const filteredSegments = useMemo(() => {
    const query = segmentQuery.trim().toLocaleLowerCase();
    if (!query) return detail.segments;
    return detail.segments.filter((segment) => [segment.title, segment.segment_id, segment.clip_id, segment.scene_role]
      .some((value) => String(value || "").toLocaleLowerCase().includes(query)));
  }, [detail.segments, segmentQuery]);

  function applyState(updater: (current: ColorState) => ColorState) {
    setState((current) => updater(current));
    setPreviews([]);
  }

  function resetAll() {
    setState(cloneColorState(baseline));
    setPreviews([]);
    setProjectMessage("已放棄尚未儲存的調色設定。");
  }

  function updateApplied(field: keyof ColorAdjustment, value: string | number) {
    applyState((current) => ({
      ...current,
      applied: {
        ...current.applied,
        [field]: isStringAdjustment(field) ? String(value) : Number(value),
      },
    }));
  }

  function applySuggestedToProject() {
    applyState((current) => ({ ...current, applied: { ...current.suggested } }));
  }

  function updateSegment(segmentId: string, patch: Partial<Pick<ColorSegmentState, "enabled" | "locked" | "excluded">>) {
    applyState((current) => {
      const existing = current.segments[segmentId] || {
        enabled: true,
        locked: false,
        excluded: false,
        applied: { ...current.applied },
      };
      return {
        ...current,
        segments: {
          ...current.segments,
          [segmentId]: { ...existing, ...patch },
        },
      };
    });
  }

  function updateSegmentApplied(segmentId: string, field: keyof ColorAdjustment, value: string | number) {
    applyState((current) => {
      const existing = current.segments[segmentId] || {
        enabled: true,
        locked: false,
        excluded: false,
      };
      return {
        ...current,
        segments: {
          ...current.segments,
          [segmentId]: {
            ...existing,
            applied: {
              ...(existing.applied || current.applied),
              [field]: isStringAdjustment(field) ? String(value) : Number(value),
            },
          },
        },
      };
    });
  }

  function applySegmentSuggestion(segmentId: string) {
    applyState((current) => {
      const effective = effectiveSegment(current, segmentId);
      return {
        ...current,
        segments: {
          ...current.segments,
          [segmentId]: {
            ...effective,
            applied: { ...(effective.suggested || current.suggested) },
          },
        },
      };
    });
  }

  function resetSegment(segmentId: string) {
    applyState((current) => {
      const segments = { ...current.segments };
      delete segments[segmentId];
      return { ...current, segments };
    });
  }

  async function analyze(force = false) {
    if (busy || dirty) {
      if (dirty) setProjectMessage("請先儲存或放棄調色變更，再重新分析核心畫面。");
      return;
    }
    const baseRevision = requireProjectRevision();
    if (baseRevision === null) return;
    const mutation = controls.beginProjectMutation(detail.project.id, "color");
    if (!mutation) {
      setProjectMessage(`目前正在${mutationLabel("color")}，請完成後再執行其他操作。`);
      return;
    }
    setBusy("analyze");
    setProjectMessage(force ? "正在忽略快取並重跑色彩分析…" : "正在分析核心畫面色彩…");
    try {
      const result = await api.colorAnalyze(detail.project.id, force, baseRevision);
      if (!result.ok || !result.state) {
        setProjectMessage(`色彩分析失敗：${result.error || "未知錯誤"}`);
        return;
      }
      const analyzed = cloneColorState(result.state);
      const successMessage = "色彩分析完成，請確認建議值與基準畫面。";
      if (controls.isCurrentProject(detail.project.id)) {
        setState(analyzed);
        setBaseline(analyzed);
        setPreviews([]);
        setProjectMessage(successMessage);
      }
      try {
        await refreshProject({ forceFresh: true, throwOnError: true });
      } catch (refreshError) {
        setProjectMessage(`${successMessage}，但畫面更新失敗：${errorMessage(refreshError)}`);
      }
    } catch (error) {
      if (controls.isCurrentProject(detail.project.id)) setMessage(`色彩分析失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
      controls.finishProjectMutation(mutation);
    }
  }

  async function changeReference(referenceId: string) {
    if (!referenceId || busy || dirty) {
      if (dirty) setProjectMessage("請先儲存或放棄調色變更，再切換色彩基準。");
      return;
    }
    const baseRevision = requireProjectRevision();
    if (baseRevision === null) return;
    const mutation = controls.beginProjectMutation(detail.project.id, "color");
    if (!mutation) {
      setProjectMessage(`目前正在${mutationLabel("color")}，請完成後再執行其他操作。`);
      return;
    }
    setBusy("reference");
    setProjectMessage("正在更新色彩基準…");
    try {
      const result = await api.colorReference(detail.project.id, referenceId, baseRevision);
      if (!result.ok || !result.state) {
        setProjectMessage(`色彩基準更新失敗：${result.error || "未知錯誤"}`);
        return;
      }
      const referenced = cloneColorState(result.state);
      const successMessage = "色彩基準已更新，請重新確認建議值。";
      if (controls.isCurrentProject(detail.project.id)) {
        setState(referenced);
        setBaseline(referenced);
        setPreviews([]);
        setProjectMessage(successMessage);
      }
      try {
        await refreshProject({ forceFresh: true, throwOnError: true });
      } catch (refreshError) {
        setProjectMessage(`${successMessage}，但畫面更新失敗：${errorMessage(refreshError)}`);
      }
    } catch (error) {
      if (controls.isCurrentProject(detail.project.id)) setMessage(`色彩基準更新失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
      controls.finishProjectMutation(mutation);
    }
  }

  async function save() {
    if (!dirty || busy || requiresLutPath) return;
    const baseRevision = requireProjectRevision();
    if (baseRevision === null) return;
    const mutation = controls.beginProjectMutation(detail.project.id, "color");
    if (!mutation) {
      setProjectMessage(`目前正在${mutationLabel("color")}，請完成後再執行其他操作。`);
      return;
    }
    setBusy("save");
    setProjectMessage("正在儲存調色設定…");
    try {
      const result = await api.colorSettings(detail.project.id, toColorStatePatch(state), baseRevision);
      if (!result.ok || !result.state) {
        setProjectMessage(`色彩設定儲存失敗：${result.error || "未知錯誤"}`);
        return;
      }
      const saved = cloneColorState(result.state);
      const successMessage = "色彩設定已儲存，專案已回到待審。";
      if (controls.isCurrentProject(detail.project.id)) {
        setState(saved);
        setBaseline(saved);
        setPreviews([]);
        setProjectMessage(successMessage);
      }
      try {
        await refreshProject({ forceFresh: true, throwOnError: true });
      } catch (refreshError) {
        setProjectMessage(`${successMessage}，但畫面更新失敗：${errorMessage(refreshError)}`);
      }
    } catch (error) {
      if (controls.isCurrentProject(detail.project.id)) setMessage(`色彩設定儲存失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
      controls.finishProjectMutation(mutation);
    }
  }

  async function preview(force = false) {
    if (busy || dirty) {
      if (dirty) setProjectMessage("調色預覽目前使用已儲存設定；請先儲存或放棄草稿。");
      return;
    }
    const baseRevision = requireProjectRevision();
    if (baseRevision === null) return;
    setBusy("preview");
    setProjectMessage(force ? "正在忽略快取並重新產生調色預覽…" : "正在產生 Before / After 調色預覽…");
    try {
      const result = await api.colorPreviewDirect(detail.project.id, force, baseRevision);
      if (!result.ok) {
        setPreviews([]);
        setProjectMessage(`調色預覽失敗：${result.error || "未知錯誤"}`);
        return;
      }
      if (controls.isCurrentProject(detail.project.id)) {
        setPreviews(result.previews || []);
        setProjectMessage(force ? "調色預覽已忽略快取重新產生。" : "Before / After 調色預覽已完成。");
      }
    } catch (error) {
      if (controls.isCurrentProject(detail.project.id)) {
        setPreviews([]);
        setProjectMessage(`調色預覽失敗：${errorMessage(error)}`);
      }
    } finally {
      setBusy("");
    }
  }

  return <div className="color-workspace">
    <section className="color-project-panel">
      <header className="color-workspace-header">
        <div>
          <span>COLOR CONSISTENCY</span>
          <h3>色彩一致性與調色預覽</h3>
          <p>分析核心畫面、設定專案基準，再逐片段處理必要例外。</p>
        </div>
        <div className="color-header-actions">
          {dirty && <strong>有未儲存變更</strong>}
          <button type="button" disabled={workspaceBusy || !dirty} onClick={resetAll}>放棄變更</button>
          <button type="button" className="good" disabled={workspaceBusy || !dirty || requiresLutPath} onClick={() => void save()}>{busy === "save" ? "儲存中…" : "儲存調色設定"}</button>
        </div>
      </header>

      <div className="color-analysis-toolbar">
        <button type="button" disabled={workspaceBusy || dirty} onClick={() => void analyze(false)}>{busy === "analyze" ? "分析中…" : "分析核心畫面"}</button>
        <button type="button" disabled={workspaceBusy || dirty} onClick={() => void analyze(true)}>忽略快取重跑分析</button>
        <span>{dirty ? "先處理未儲存變更，才能避免分析結果覆蓋草稿。" : state.analysis.basis_text || "尚未分析色彩基準。"}</span>
      </div>

      <label className="color-toggle"><input type="checkbox" disabled={workspaceBusy} checked={state.enabled} onChange={(event) => applyState((current) => ({ ...current, enabled: event.target.checked }))} /> 啟用專案色彩一致性</label>

      <div className="color-overview-grid">
        <section className="color-reference-card">
          <div className="color-section-title"><b>色彩基準</b><span>基準變更會重新計算建議值</span></div>
          <label>Reference Clip / Frame<select aria-label="色彩基準" disabled={workspaceBusy || dirty || state.references.length === 0} value={selectedReferenceId} onChange={(event) => void changeReference(event.target.value)}><option value="">尚未選擇基準畫面</option>{state.references.map((reference) => <option key={reference.id} value={reference.id}>{reference.type === "segment" ? "片段" : "畫格"} · {reference.label || "未命名"} · {reference.score.toFixed(2)}</option>)}</select></label>
          {isColorReference(state.reference) && state.reference.frame_url
            ? <img src={state.reference.frame_url} alt="色彩基準畫面" />
            : <div className="color-reference-empty">尚無基準畫面縮圖</div>}
          <div className="color-analysis-meta">
            <span>平均亮度 <b>{Number(state.analysis.luma?.average || 0).toFixed(1)}</b></span>
            <span>高光比例 <b>{(Number(state.analysis.luma?.highlight_ratio || 0) * 100).toFixed(1)}%</b></span>
            <span>分析信心 <b>{state.analysis.confidence || "未分析"}</b></span>
          </div>
          {state.analysis.warnings?.length ? <div className="color-warning">{state.analysis.warnings.map((warning) => <span key={warning}>{warning}</span>)}</div> : null}
        </section>

        <section className="color-project-settings">
          <div className="color-section-title"><b>專案套用值</b><span>正式輸出與預覽使用已儲存的套用值</span></div>
          <div className="color-form-grid">
            <label>技術模式<select aria-label="技術 LUT 模式" disabled={workspaceBusy} value={state.applied.mode} onChange={(event) => updateApplied("mode", event.target.value)}><option value="dji_dlog_m">DJI D-Log M</option><option value="dji_dlog">DJI D-Log</option><option value="dji_lut">自訂 DJI LUT</option><option value="safe_restore">保守修正</option><option value="manual">手動調整</option><option value="none">不套用</option></select></label>
            <label className="wide">LUT 路徑<input aria-label="LUT 路徑" disabled={workspaceBusy || state.applied.mode !== "dji_lut"} value={state.applied.lut_path} onChange={(event) => updateApplied("lut_path", event.target.value)} placeholder=".cube LUT 路徑" /></label>
          </div>
          {requiresLutPath && <div className="color-warning">自訂 DJI LUT 模式需要有效的 .cube 路徑。</div>}
          <div className="color-adjustment-grid">
            {adjustmentFields.map((field) => <label key={field}>{adjustmentLabel(field)}<input aria-label={`專案${adjustmentLabel(field)}`} disabled={workspaceBusy} type="number" step="0.01" value={state.applied[field]} onChange={(event) => updateApplied(field, event.target.value)} /></label>)}
          </div>
          <div className="color-suggestion">
            <div><b>系統建議值</b><span>曝光 {state.suggested.exposure} · 色溫 {state.suggested.temperature} · 色調 {state.suggested.tint} · 對比 {state.suggested.contrast} · 飽和 {state.suggested.saturation}</span></div>
            <button type="button" disabled={workspaceBusy} onClick={applySuggestedToProject}>套用全部建議</button>
          </div>
        </section>
      </div>

      <div className="color-preview-toolbar">
        <button type="button" disabled={workspaceBusy || dirty} onClick={() => void preview(false)}>{busy === "preview" ? "預覽產生中…" : "產生 Before / After 預覽"}</button>
        <button type="button" disabled={workspaceBusy || dirty} onClick={() => void preview(true)}>忽略快取重跑預覽</button>
        <span>{dirty ? "請先儲存或放棄草稿。" : "預覽使用目前已儲存的調色設定。"}</span>
      </div>

      {previews.length > 0 && <div className="color-preview-grid" aria-label="調色預覽結果">
        {previews.map((item) => <article key={`${item.video_id}-${item.segment_id}`}>
          <header><b>{item.segment_id}</b><span>素材 #{item.video_id} · {item.cache_hit ? "快取" : "新產生"}{item.confidence != null ? ` · 信心 ${item.confidence.toFixed(2)}` : ""}</span></header>
          <div><figure><figcaption>Before</figcaption><video controls preload="metadata" src={item.before_url} /></figure><figure><figcaption>After</figcaption><video controls preload="metadata" src={item.after_url} /></figure></div>
          {item.warnings?.length ? <p>{item.warnings.join(" · ")}</p> : null}
        </article>)}
      </div>}
    </section>

    <section className="color-segment-panel">
      <header className="color-segment-heading">
        <div><h3>片段色彩覆寫</h3><p>未自訂的片段會繼承專案套用值。</p></div>
        <label><span>搜尋片段</span><input type="search" aria-label="搜尋調色片段" value={segmentQuery} onChange={(event) => setSegmentQuery(event.target.value)} placeholder="標題、片段或素材編號" /></label>
      </header>
      <div className="color-segment-list">
        {filteredSegments.map((segment) => <ColorSegmentRow
          key={segment.segment_id}
          segment={segment}
          state={state}
          item={effectiveSegment(state, segment.segment_id)}
          customized={Boolean(state.segments[segment.segment_id])}
          busy={workspaceBusy}
          onToggle={(patch) => updateSegment(segment.segment_id, patch)}
          onAdjustment={(field, value) => updateSegmentApplied(segment.segment_id, field, value)}
          onSuggestion={() => applySegmentSuggestion(segment.segment_id)}
          onReset={() => resetSegment(segment.segment_id)}
        />)}
        {filteredSegments.length === 0 && <div className="color-empty">找不到符合條件的片段。</div>}
      </div>
    </section>
  </div>;
}

function ColorSegmentRow({ segment, state, item, customized, busy, onToggle, onAdjustment, onSuggestion, onReset }: {
  segment: Segment;
  state: ColorState;
  item: ColorSegmentState;
  customized: boolean;
  busy: boolean;
  onToggle: (patch: Partial<Pick<ColorSegmentState, "enabled" | "locked" | "excluded">>) => void;
  onAdjustment: (field: keyof ColorAdjustment, value: string | number) => void;
  onSuggestion: () => void;
  onReset: () => void;
}) {
  const disabled = item.excluded || !item.enabled;
  return <article className={`color-segment-row${disabled ? " disabled" : ""}`}>
    <header>
      <div><b>{segment.title || segment.segment_id}</b><span>{segment.clip_id} · {customized ? "片段自訂" : "專案預設"} · 信心 {Number(item.confidence || 0).toFixed(2)}</span></div>
      <div className="color-segment-toggles">
        <label><input type="checkbox" disabled={busy} checked={item.enabled} onChange={(event) => onToggle({ enabled: event.target.checked })} />啟用</label>
        <label><input type="checkbox" disabled={busy} checked={item.locked} onChange={(event) => onToggle({ locked: event.target.checked })} />鎖定</label>
        <label><input type="checkbox" disabled={busy} checked={item.excluded} onChange={(event) => onToggle({ excluded: event.target.checked })} />排除</label>
      </div>
    </header>
    {item.warnings?.length ? <div className="color-segment-warning">{item.warnings.join(" · ")}</div> : null}
    <details>
      <summary>片段色彩值</summary>
      <div className="color-adjustment-grid">
        {adjustmentFields.map((field) => <label key={field}>{adjustmentLabel(field)}<input aria-label={`${segment.title || segment.segment_id} ${adjustmentLabel(field)}`} disabled={busy || disabled} type="number" step="0.01" value={item.applied?.[field] ?? state.applied[field]} onChange={(event) => onAdjustment(field, event.target.value)} /></label>)}
      </div>
    </details>
    <footer>
      <button type="button" disabled={busy || disabled} onClick={onSuggestion}>套用片段建議</button>
      <button type="button" disabled={busy || !customized} onClick={onReset}>恢復專案預設</button>
    </footer>
  </article>;
}

const adjustmentFields = ["exposure", "temperature", "tint", "contrast", "highlights", "shadows", "saturation", "gamma"] as const;

function effectiveSegment(state: ColorState, segmentId: string): ColorSegmentState {
  return state.segments[segmentId] || {
    enabled: true,
    locked: false,
    excluded: false,
    suggested: state.suggested,
    applied: state.applied,
    confidence: 0,
    warnings: [],
  };
}

function toColorStatePatch(state: ColorState): ColorStatePatch {
  return {
    schema_version: state.schema_version,
    enabled: state.enabled,
    applied: { ...state.applied },
    segments: Object.fromEntries(Object.entries(state.segments).map(([segmentId, segment]) => [segmentId, {
      enabled: segment.enabled,
      locked: segment.locked,
      excluded: segment.excluded,
      ...(segment.applied ? { applied: { ...segment.applied } } : {}),
    }])),
  };
}

function colorSignature(state: ColorState): string {
  return JSON.stringify({
    enabled: state.enabled,
    applied: state.applied,
    segments: state.segments,
  });
}

function cloneColorState(state: ColorState): ColorState {
  return JSON.parse(JSON.stringify(state)) as ColorState;
}

function emptyColorState(): ColorState {
  return {
    schema_version: 2,
    enabled: true,
    reference: {},
    references: [],
    analysis: {},
    suggested: { ...EMPTY_ADJUSTMENT },
    applied: { ...EMPTY_ADJUSTMENT },
    segments: {},
  };
}

function isColorReference(reference: ColorState["reference"]): reference is Exclude<ColorState["reference"], Record<string, never>> {
  return Boolean(reference && "id" in reference && reference.id);
}

function isStringAdjustment(field: keyof ColorAdjustment): boolean {
  return field === "mode" || field === "lut_path" || field === "lut_kind";
}

function adjustmentLabel(field: keyof ColorAdjustment): string {
  return ({
    exposure: "曝光",
    temperature: "白平衡色溫",
    tint: "白平衡色調",
    contrast: "對比",
    highlights: "高光",
    shadows: "陰影",
    saturation: "飽和度",
    gamma: "Gamma",
  } as Record<string, string>)[field] || field;
}

function errorMessage(error: unknown): string {
  return formatApiError(error);
}
