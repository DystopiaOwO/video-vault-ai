import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type AudioSegmentSettings,
  type ColorSegmentPatch,
  type ProjectDetail,
  type StoryboardState,
} from "../../api";
import {
  StoryboardReviewWorkspace,
  type SegmentTimingPatch,
  type StoryboardPreviewItem,
  type StoryboardPreviewMode,
} from "./StoryboardReviewWorkspace";
import {
  addStoryboardGroup,
  buildStoryboardViewModel,
  deleteEmptyStoryboardGroup,
  editableStoryboardState,
  moveStoryboardGroup,
  moveStoryboardSegment,
  normalizeStoryboardState,
  renameStoryboardGroup,
  timelineStartForSegment,
  updateStoryboardSegment,
  type StoryboardSegmentEdit,
} from "./storyboardViewModel";

export type StoryboardWorkspaceControllerProps = {
  detail: ProjectDetail;
  setMessage: (message: string) => void;
  refreshProject: (options?: { forceFresh?: boolean; jobs?: boolean }) => Promise<unknown>;
};

type TimingDraft = { startSeconds: number; endSeconds: number; speed: number };
type BusyAction = "" | "save" | "regenerate" | "timing" | "preview" | "thumbnail" | "audio" | "color";

export function StoryboardWorkspaceController({
  detail,
  setMessage,
  refreshProject,
}: StoryboardWorkspaceControllerProps) {
  const [state, setState] = useState<StoryboardState>(() => editableStoryboardState(detail));
  const [selectedId, setSelectedId] = useState(() => firstSegmentId(detail));
  const [timingDrafts, setTimingDrafts] = useState<Record<string, TimingDraft>>(() => timingFromDetail(detail));
  const [previewItems, setPreviewItems] = useState<StoryboardPreviewItem[]>([]);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<BusyAction>("");
  const dirtyRef = useRef(false);
  const projectIdRef = useRef(detail.project.id);

  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => {
    const projectChanged = projectIdRef.current !== detail.project.id;
    if (projectChanged) {
      projectIdRef.current = detail.project.id;
      dirtyRef.current = false;
      setDirty(false);
      setState(editableStoryboardState(detail));
      setTimingDrafts(timingFromDetail(detail));
      setSelectedId(firstSegmentId(detail));
      setPreviewItems([]);
      setBusy("");
      return;
    }

    if (!dirtyRef.current) setState(editableStoryboardState(detail));
    setTimingDrafts((current) => mergeServerTiming(current, detail));
    setSelectedId((current) => current && detail.segments.some((segment) => segment.segment_id === current)
      ? current
      : firstSegmentId(detail));
  }, [detail]);

  const model = useMemo(
    () => buildStoryboardViewModel({ ...detail, storyboard: state }),
    [detail, state],
  );

  function replaceLocalState(next: StoryboardState) {
    setState(normalizeStoryboardState(next));
    setDirty(true);
  }

  function changeStoryboard(segmentId: string, patch: Partial<StoryboardSegmentEdit>) {
    replaceLocalState(updateStoryboardSegment(state, segmentId, patch));
  }

  function changeTiming(segmentId: string, patch: SegmentTimingPatch) {
    setTimingDrafts((current) => {
      const source = current[segmentId] || timingForSegment(detail, segmentId);
      return { ...current, [segmentId]: { ...source, ...patch } };
    });
  }

  async function saveStoryboard() {
    if (!dirty || busy) return;
    const normalized = normalizeStoryboardState(state);
    setBusy("save");
    setMessage("正在儲存分鏡…");
    try {
      const result = await api.updateStoryboard(detail.project.id, normalized);
      if (!result.ok || !result.storyboard) {
        setMessage(`分鏡儲存失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setState(editableStoryboardState({ ...detail, storyboard: result.storyboard }));
      dirtyRef.current = false;
      setDirty(false);
      setMessage(result.approval_invalidated
        ? "分鏡已儲存，輸出內容有變更，請重新核准後再正式輸出。"
        : "分鏡已儲存，這次未修改輸出內容，既有核准仍有效。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`分鏡儲存失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
    }
  }

  async function regenerateStoryboard() {
    if (dirty || busy) return;
    setBusy("regenerate");
    setMessage(model.exists ? "正在重新產生分鏡…" : "正在建立分鏡…");
    try {
      const result = await api.generateStoryboard(detail.project.id, model.exists);
      if (!result.ok || !result.storyboard) {
        setMessage(`分鏡產生失敗：${result.error || "未知錯誤"}`);
        return;
      }
      const generated = editableStoryboardState({ ...detail, storyboard: result.storyboard });
      setState(generated);
      setTimingDrafts(timingFromDetail(detail));
      setSelectedId(firstStoryboardSegmentId(generated) || firstSegmentId(detail));
      setPreviewItems([]);
      dirtyRef.current = false;
      setDirty(false);
      setMessage(model.exists
        ? "分鏡已重新產生，鎖定片段、人工排序、備註與自訂群組已保留。"
        : "分鏡已建立，請開始審核與排序。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`分鏡產生失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
    }
  }

  async function saveTiming(segmentId: string) {
    if (busy) return;
    const timing = timingDrafts[segmentId] || timingForSegment(detail, segmentId);
    setBusy("timing");
    setMessage("正在儲存片段剪點…");
    try {
      const result = await api.saveSegmentTiming(detail.project.id, segmentId, {
        start_seconds: timing.startSeconds,
        end_seconds: timing.endSeconds,
        speed: timing.speed,
      });
      if (!result.ok) {
        setMessage(`剪點儲存失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setMessage("片段剪點已儲存，輸出內容有變更，請重新核准。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`剪點儲存失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
    }
  }

  async function preview(segmentId: string, mode: StoryboardPreviewMode, force = false) {
    if (busy) return;
    setBusy("preview");
    setMessage(force
      ? "正在忽略快取並重新產生分鏡預覽…"
      : mode === "segment"
        ? "正在產生片段短預覽…"
        : mode === "range"
          ? "正在產生分鏡範圍預覽…"
          : "正在產生片段銜接預覽…");
    try {
      const result = await api.storyboardPreview(detail.project.id, {
        mode,
        segmentId,
        durationSeconds: mode === "segment" ? 5 : 8,
        timelineStartSeconds: mode === "range" ? timelineStartForSegment(state, detail, segmentId, timingDrafts) : 0,
        storyboardState: state,
        force,
      });
      if (!result.ok) {
        setMessage(`預覽產生失敗：${result.error || "未知錯誤"}`);
        return;
      }
      const items = result.previews?.map((item) => ({
        kind: item.kind,
        url: item.url,
        durationSeconds: Number(item.duration_seconds || 0),
      })) || (result.url ? [{
        kind: mode,
        url: result.url,
        durationSeconds: Number(result.duration_seconds || 0),
      }] : []);
      setPreviewItems(items);
      setMessage(force
        ? `${previewModeLabel(mode)}已忽略快取重新產生，共 ${items.length} 個預覽。`
        : result.cache_hit
          ? `${previewModeLabel(mode)}已從快取載入，共 ${items.length} 個預覽。`
          : `${previewModeLabel(mode)}已完成，共 ${items.length} 個預覽。`);
    } catch (error) {
      setMessage(`預覽產生失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
    }
  }

  async function generateThumbnail(segmentId: string, ratio: number, force = false) {
    if (busy) return;
    setBusy("thumbnail");
    setMessage(force ? "正在忽略快取並重新產生代表畫格…" : "正在產生代表畫格…");
    try {
      const result = await api.storyboardThumbnail(detail.project.id, segmentId, ratio, force);
      if (!result.ok) {
        setMessage(`代表畫格產生失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setState((current) => updateStoryboardSegment(current, segmentId, {
        thumbnail_time_ratio: ratio,
        thumbnail_url: result.url,
      }));
      setDirty(true);
      setMessage(force
        ? "代表畫格已忽略快取重新產生，請儲存分鏡。"
        : result.cache_hit
          ? "代表畫格已從快取載入，請儲存分鏡。"
          : "代表畫格已產生，請儲存分鏡以保留位置。");
    } catch (error) {
      setMessage(`代表畫格產生失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
    }
  }

  async function changeAudioRole(segmentId: string, role: AudioSegmentSettings["role"] | "default") {
    if (busy) return;
    setBusy("audio");
    try {
      const existing = detail.audio.segments[segmentId];
      const segmentPatch = role === "default"
        ? null
        : { ...(existing && typeof existing === "object" ? existing : {}), role };
      const result = await api.audioSettings(detail.project.id, { segments: { [segmentId]: segmentPatch } });
      if (!result.ok) {
        setMessage(`原音角色更新失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setMessage(role === "default" ? "片段已改回專案音訊預設。" : "片段原音角色已更新，請重新確認預覽。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`原音角色更新失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
    }
  }

  async function toggleColor(segmentId: string) {
    if (busy) return;
    const segment = model.segments.find((item) => item.id === segmentId);
    if (!segment) return;
    setBusy("color");
    try {
      const existing = detail.color.segments[segmentId];
      const patch: ColorSegmentPatch = {
        enabled: !segment.colorEnabled,
        locked: existing?.locked ?? false,
        excluded: false,
        ...(existing?.applied ? { applied: { ...existing.applied } } : {}),
      };
      const result = await api.colorSettings(detail.project.id, {
        schema_version: detail.color.schema_version,
        enabled: detail.color.enabled,
        applied: { ...detail.color.applied },
        segments: { [segmentId]: patch },
      });
      if (!result.ok) {
        setMessage(`片段調色更新失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setMessage(!segment.colorEnabled ? "已啟用此片段調色。" : "已停用此片段調色。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`片段調色更新失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
    }
  }

  async function resetColor(segmentId: string) {
    if (busy) return;
    setBusy("color");
    try {
      const result = await api.colorSettings(detail.project.id, {
        schema_version: detail.color.schema_version,
        enabled: detail.color.enabled,
        applied: { ...detail.color.applied },
        segments: { [segmentId]: null },
      });
      if (!result.ok) {
        setMessage(`恢復調色預設失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setMessage("已恢復此片段的專案調色預設。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`恢復調色預設失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
    }
  }

  return <StoryboardReviewWorkspace
    model={model}
    selectedId={selectedId}
    dirty={dirty}
    saving={busy === "save"}
    regenerating={busy === "regenerate"}
    previewing={busy === "preview"}
    thumbnailing={busy === "thumbnail"}
    timingDrafts={timingDrafts}
    previewItems={previewItems}
    onSelect={setSelectedId}
    onStoryboardChange={changeStoryboard}
    onTimingChange={changeTiming}
    onSaveTiming={(segmentId) => void saveTiming(segmentId)}
    onSave={() => void saveStoryboard()}
    onRegenerate={() => void regenerateStoryboard()}
    onPreview={(segmentId, mode, force) => void preview(segmentId, mode, force)}
    onAudioRoleChange={(segmentId, role) => void changeAudioRole(segmentId, role)}
    onToggleColor={(segmentId) => void toggleColor(segmentId)}
    onResetColor={(segmentId) => void resetColor(segmentId)}
    onGenerateThumbnail={(segmentId, ratio, force) => void generateThumbnail(segmentId, ratio, force)}
    onMoveSegment={(segmentId, delta) => replaceLocalState(moveStoryboardSegment(state, segmentId, delta))}
    onAddGroup={(title) => replaceLocalState(addStoryboardGroup(state, title))}
    onRenameGroup={(groupId, title) => replaceLocalState(renameStoryboardGroup(state, groupId, title))}
    onMoveGroup={(groupId, delta) => replaceLocalState(moveStoryboardGroup(state, groupId, delta))}
    onDeleteGroup={(groupId) => replaceLocalState(deleteEmptyStoryboardGroup(state, groupId))}
  />;
}

function timingFromDetail(detail: ProjectDetail): Record<string, TimingDraft> {
  return Object.fromEntries(detail.segments.map((segment) => [segment.segment_id, {
    startSeconds: segment.start_seconds,
    endSeconds: segment.end_seconds,
    speed: segment.speed || 1,
  }]));
}

function mergeServerTiming(current: Record<string, TimingDraft>, detail: ProjectDetail): Record<string, TimingDraft> {
  const next = { ...current };
  for (const segment of detail.segments) {
    if (!next[segment.segment_id]) next[segment.segment_id] = {
      startSeconds: segment.start_seconds,
      endSeconds: segment.end_seconds,
      speed: segment.speed || 1,
    };
  }
  for (const segmentId of Object.keys(next)) {
    if (!detail.segments.some((segment) => segment.segment_id === segmentId)) delete next[segmentId];
  }
  return next;
}

function timingForSegment(detail: ProjectDetail, segmentId: string): TimingDraft {
  const segment = detail.segments.find((item) => item.segment_id === segmentId);
  return {
    startSeconds: segment?.start_seconds ?? 0,
    endSeconds: segment?.end_seconds ?? 0,
    speed: segment?.speed || 1,
  };
}

function firstSegmentId(detail: ProjectDetail): string {
  return firstStoryboardSegmentId(editableStoryboardState(detail)) || detail.segments[0]?.segment_id || "";
}

function firstStoryboardSegmentId(state: StoryboardState): string {
  for (const group of [...(state.groups || [])].sort((left, right) => left.order - right.order)) {
    const segmentId = Object.entries(state.segments || {})
      .filter(([, segment]) => segment.group_id === group.group_id)
      .sort(([, left], [, right]) => left.order - right.order)
      .map(([id]) => id)[0];
    if (segmentId) return segmentId;
  }
  return "";
}

function previewModeLabel(mode: StoryboardPreviewMode): string {
  if (mode === "transition") return "片段銜接預覽";
  if (mode === "range") return "分鏡範圍預覽";
  return "片段預覽";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "網路或服務錯誤";
}
