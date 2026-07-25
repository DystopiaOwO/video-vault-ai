import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  formatApiError,
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
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import { createProjectMutationControls, mutationLabel, ProjectMutationCoordinator, type ProjectMutationControls, type ProjectMutation } from "../../projectMutation";

export type StoryboardWorkspaceControllerProps = {
  detail: ProjectDetail;
  setMessage: (message: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<unknown>;
  mutationControls?: ProjectMutationControls;
};

type TimingDraft = { startSeconds: number; endSeconds: number; speed: number };
type BusyAction = "" | "save" | "regenerate" | "timing" | "preview" | "thumbnail" | "audio" | "color";

export function StoryboardWorkspaceController({
  detail,
  setMessage,
  refreshProject,
  mutationControls,
}: StoryboardWorkspaceControllerProps) {
  const [state, setState] = useState<StoryboardState>(() => editableStoryboardState(detail));
  const [selectedId, setSelectedId] = useState(() => firstSegmentId(detail));
  const [timingDrafts, setTimingDrafts] = useState<Record<string, TimingDraft>>(() => timingFromDetail(detail));
  const [timingDirty, setTimingDirty] = useState<Record<string, boolean>>({});
  const committedTimingsRef = useRef<Record<string, TimingDraft>>(timingFromDetail(detail));
  const serverTimingSnapshotRef = useRef<Record<string, TimingDraft>>(timingFromDetail(detail));
  const awaitingTimingAckRef = useRef<Set<string>>(new Set());
  const [previewItems, setPreviewItems] = useState<StoryboardPreviewItem[]>([]);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<BusyAction>("");
  const dirtyRef = useRef(false);
  const timingDirtyRef = useRef<Record<string, boolean>>({});
  const projectIdRef = useRef(detail.project.id);
  const fallbackControlsRef = useRef<ProjectMutationControls | null>(null);
  if (!fallbackControlsRef.current) fallbackControlsRef.current = createProjectMutationControls(new ProjectMutationCoordinator());
  const controls = mutationControls || fallbackControlsRef.current;
  const hasUnsavedTiming = Object.values(timingDirty).some(Boolean);
  const projectMutationBusy = controls.isProjectMutationBusy(detail.project.id);
  const workspaceBusy = Boolean(busy) || projectMutationBusy;

  function setProjectMessage(message: string) {
    if (controls.isCurrentProject(detail.project.id)) setMessage(message);
  }

  function beginMutation(mutation: ProjectMutation) {
    const token = controls.beginProjectMutation(detail.project.id, mutation);
    if (!token) setProjectMessage(`目前正在${mutationLabel(mutation)}，請完成後再執行其他操作。`);
    return token;
  }

  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => {
    timingDirtyRef.current = timingDirty;
  }, [timingDirty]);

  useEffect(() => {
    if (!dirty && !hasUnsavedTiming) return;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [dirty, hasUnsavedTiming]);

  useEffect(() => {
    const projectChanged = projectIdRef.current !== detail.project.id;
    if (projectChanged) {
      projectIdRef.current = detail.project.id;
      dirtyRef.current = false;
      timingDirtyRef.current = {};
      setDirty(false);
      setTimingDirty({});
      setState(editableStoryboardState(detail));
      setTimingDrafts(timingFromDetail(detail));
      committedTimingsRef.current = timingFromDetail(detail);
      serverTimingSnapshotRef.current = timingFromDetail(detail);
      awaitingTimingAckRef.current = new Set();
      setSelectedId(firstSegmentId(detail));
      setPreviewItems([]);
      setBusy("");
      return;
    }

    if (!dirtyRef.current) setState(editableStoryboardState(detail));
    const incomingTimings = timingFromDetail(detail);
    const previousServerTimings = serverTimingSnapshotRef.current;
    const nextCommittedTimings = { ...committedTimingsRef.current };
    for (const segment of detail.segments) {
      const id = segment.segment_id;
      const incoming = incomingTimings[id];
      const previousServer = previousServerTimings[id];
      const awaitingAck = awaitingTimingAckRef.current.has(id);
      if (timingDirtyRef.current[id]) {
        if (!awaitingAck && (!previousServer || !sameTiming(incoming, previousServer))) {
          nextCommittedTimings[id] = incoming;
        }
        continue;
      }
      // Preserve a just-saved local commit when a stale GET arrives. A real
      // server change is accepted once it differs from the last server snapshot.
      if (!previousServer || !sameTiming(incoming, previousServer)) {
        nextCommittedTimings[id] = incoming;
        awaitingTimingAckRef.current.delete(id);
      } else if (!nextCommittedTimings[id] && !awaitingAck) {
        nextCommittedTimings[id] = incoming;
      }
    }
    committedTimingsRef.current = nextCommittedTimings;
    serverTimingSnapshotRef.current = incomingTimings;
    setTimingDrafts((current) => syncServerTiming(current, detail, timingDirtyRef.current, nextCommittedTimings));
    setSelectedId((current) => current && detail.segments.some((segment) => segment.segment_id === current)
      ? current
      : firstSegmentId(detail));
  }, [detail]);

  const model = useMemo(
    () => buildStoryboardViewModel({ ...detail, storyboard: state }),
    [detail, state],
  );

  function setStoryboardDirty(value: boolean) {
    dirtyRef.current = value;
    setDirty(value);
  }

  function setSegmentTimingDirty(segmentId: string, value: boolean) {
    setTimingDirty((current) => {
      const next = { ...current };
      if (value) next[segmentId] = true;
      else delete next[segmentId];
      timingDirtyRef.current = next;
      return next;
    });
  }

  function replaceLocalState(next: StoryboardState) {
    setState(normalizeStoryboardState(next));
    setStoryboardDirty(true);
    setPreviewItems([]);
  }

  function changeStoryboard(segmentId: string, patch: Partial<StoryboardSegmentEdit>) {
    replaceLocalState(updateStoryboardSegment(state, segmentId, patch));
  }

  function changeTiming(segmentId: string, patch: SegmentTimingPatch) {
    const source = timingDrafts[segmentId] || committedTimingForSegment(committedTimingsRef.current, detail, segmentId);
    const next = { ...source, ...patch };
    setTimingDrafts((current) => ({ ...current, [segmentId]: { ...(current[segmentId] || source), ...patch } }));
    setSegmentTimingDirty(segmentId, !sameTiming(next, committedTimingForSegment(committedTimingsRef.current, detail, segmentId)));
    setPreviewItems([]);
  }

  function resetTiming(segmentId: string) {
    const committed = awaitingTimingAckRef.current.has(segmentId)
      ? committedTimingForSegment(committedTimingsRef.current, detail, segmentId)
      : serverTimingSnapshotRef.current[segmentId]
        || committedTimingForSegment(committedTimingsRef.current, detail, segmentId);
    setTimingDrafts((current) => ({ ...current, [segmentId]: committed }));
    setSegmentTimingDirty(segmentId, false);
    setPreviewItems([]);
    setProjectMessage("已放棄此片段尚未儲存的剪點變更。");
  }

  function selectSegment(segmentId: string) {
    if (segmentId !== selectedId) setPreviewItems([]);
    setSelectedId(segmentId);
  }

  async function saveStoryboard() {
    if (!dirty || busy) return;
    const mutation = beginMutation("storyboard");
    if (!mutation) return;
    const normalized = normalizeStoryboardState(state);
    setBusy("save");
    setProjectMessage("正在儲存分鏡…");
    try {
      const result = await (detail.project_revision === undefined
        ? api.updateStoryboard(detail.project.id, normalized)
        : api.updateStoryboard(detail.project.id, normalized, detail.project_revision));
      if (!result.ok || !result.storyboard) {
        setProjectMessage(`分鏡儲存失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setState(editableStoryboardState({ ...detail, storyboard: result.storyboard }));
      setStoryboardDirty(false);
      const successMessage = result.approval_invalidated
        ? "分鏡已儲存，輸出內容有變更，請重新核准後再正式輸出。"
        : "分鏡已儲存，這次未修改輸出內容，既有核准仍有效。";
      setProjectMessage(successMessage);
      await refreshAfterMutation(successMessage);
    } catch (error) {
      setProjectMessage(`分鏡儲存失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
      controls.finishProjectMutation(mutation);
    }
  }

  async function regenerateStoryboard() {
    if (dirty || hasUnsavedTiming || busy) return;
    const mutation = beginMutation("storyboard");
    if (!mutation) return;
    setBusy("regenerate");
    setProjectMessage(model.exists ? "正在重新產生分鏡…" : "正在建立分鏡…");
    try {
      const result = await api.generateStoryboard(detail.project.id, model.exists);
      if (!result.ok || !result.storyboard) {
        setProjectMessage(`分鏡產生失敗：${result.error || "未知錯誤"}`);
        return;
      }
      const generated = editableStoryboardState({ ...detail, storyboard: result.storyboard });
      setState(generated);
      setTimingDrafts(timingFromDetail(detail));
      setTimingDirty({});
      timingDirtyRef.current = {};
      setSelectedId(firstStoryboardSegmentId(generated) || firstSegmentId(detail));
      setPreviewItems([]);
      setStoryboardDirty(false);
      const successMessage = model.exists
        ? "分鏡已重新產生，鎖定片段、人工排序、備註與自訂群組已保留。"
        : "分鏡已建立，請開始審核與排序。";
      setProjectMessage(successMessage);
      await refreshAfterMutation(successMessage);
    } catch (error) {
      setProjectMessage(`分鏡產生失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
      controls.finishProjectMutation(mutation);
    }
  }

  async function saveTiming(segmentId: string) {
    if (busy || !timingDirtyRef.current[segmentId]) return;
    const mutation = beginMutation("timing");
    if (!mutation) return;
    const timing = timingDrafts[segmentId] || committedTimingForSegment(committedTimingsRef.current, detail, segmentId);
    setBusy("timing");
    setProjectMessage("正在儲存片段剪點…");
    try {
      const timingPatch = {
        start_seconds: timing.startSeconds,
        end_seconds: timing.endSeconds,
        speed: timing.speed,
      };
      const result = await (detail.project_revision === undefined
        ? api.saveSegmentTiming(detail.project.id, segmentId, timingPatch)
        : api.saveSegmentTiming(detail.project.id, segmentId, timingPatch, detail.project_revision));
      if (!result.ok) {
        setProjectMessage(`剪點儲存失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setSegmentTimingDirty(segmentId, false);
      committedTimingsRef.current = { ...committedTimingsRef.current, [segmentId]: { ...timing } };
      awaitingTimingAckRef.current.add(segmentId);
      setPreviewItems([]);
      const successMessage = "片段剪點已儲存，輸出內容有變更，請重新核准。";
      setProjectMessage(successMessage);
      await refreshAfterMutation(successMessage);
    } catch (error) {
      setProjectMessage(`剪點儲存失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
      controls.finishProjectMutation(mutation);
    }
  }

  async function preview(segmentId: string, mode: StoryboardPreviewMode, force = false) {
    if (busy) return;
    const selectedSegment = model.segments.find((item) => item.id === segmentId);
    if (!selectedSegment?.included) {
      setProjectMessage("此片段已排除，預覽已停用，不會進入正式輸出。請先納入成片後再預覽。");
      return;
    }
    if (Object.values(timingDirtyRef.current).some(Boolean)) {
      setProjectMessage("請先儲存所有未完成的片段剪點，再產生預覽。");
      return;
    }
    setBusy("preview");
    setProjectMessage(force
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
        timelineStartSeconds: mode === "range" ? timelineStartForSegment(state, detail, segmentId, committedTimingsRef.current) : 0,
        storyboardState: state,
        force,
      });
      if (!result.ok) {
        setProjectMessage(`預覽產生失敗：${result.error || "未知錯誤"}`);
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
      setProjectMessage(force
        ? `${previewModeLabel(mode)}已忽略快取重新產生，共 ${items.length} 個預覽。`
        : result.cache_hit
          ? `${previewModeLabel(mode)}已從快取載入，共 ${items.length} 個預覽。`
          : `${previewModeLabel(mode)}已完成，共 ${items.length} 個預覽。`);
    } catch (error) {
      setProjectMessage(`預覽產生失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
    }
  }

  async function generateThumbnail(segmentId: string, ratio: number, force = false) {
    if (busy) return;
    const mutation = beginMutation("storyboard");
    if (!mutation) return;
    setBusy("thumbnail");
    setProjectMessage(force ? "正在忽略快取並重新產生代表畫格…" : "正在產生代表畫格…");
    try {
      const result = await api.storyboardThumbnail(detail.project.id, segmentId, ratio, force);
      if (!result.ok) {
        setProjectMessage(`代表畫格產生失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setState((current) => updateStoryboardSegment(current, segmentId, {
        thumbnail_time_ratio: ratio,
        thumbnail_url: result.url,
      }));
      setStoryboardDirty(true);
      setPreviewItems([]);
      setProjectMessage(force
        ? "代表畫格已忽略快取重新產生，請儲存分鏡。"
        : result.cache_hit
          ? "代表畫格已從快取載入，請儲存分鏡。"
          : "代表畫格已產生，請儲存分鏡以保留位置。");
    } catch (error) {
      setProjectMessage(`代表畫格產生失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
      controls.finishProjectMutation(mutation);
    }
  }

  async function changeAudioRole(segmentId: string, role: AudioSegmentSettings["role"] | "default") {
    if (busy) return;
    const mutation = beginMutation("audio");
    if (!mutation) return;
    setBusy("audio");
    try {
      const existing = detail.audio.segments[segmentId];
      const segmentPatch = role === "default"
        ? null
        : { ...(existing && typeof existing === "object" ? existing : {}), role };
      const result = await (detail.project_revision === undefined
        ? api.audioSettings(detail.project.id, { segments: { [segmentId]: segmentPatch } })
        : api.audioSettings(detail.project.id, { segments: { [segmentId]: segmentPatch } }, detail.project_revision));
      if (!result.ok) {
        setProjectMessage(`原音角色更新失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setPreviewItems([]);
      const successMessage = role === "default" ? "片段已改回專案音訊預設。" : "片段原音角色已更新，請重新確認預覽。";
      setProjectMessage(successMessage);
      await refreshAfterMutation(successMessage);
    } catch (error) {
      setProjectMessage(`原音角色更新失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
      controls.finishProjectMutation(mutation);
    }
  }

  async function toggleColor(segmentId: string) {
    if (busy) return;
    const segment = model.segments.find((item) => item.id === segmentId);
    if (!segment) return;
    const mutation = beginMutation("color");
    if (!mutation) return;
    setBusy("color");
    try {
      const existing = detail.color.segments[segmentId];
      const patch: ColorSegmentPatch = {
        enabled: !segment.colorEnabled,
        locked: existing?.locked ?? false,
        excluded: false,
        ...(existing?.applied ? { applied: { ...existing.applied } } : {}),
      };
      const colorPatch = {
        schema_version: detail.color.schema_version,
        enabled: detail.color.enabled,
        applied: { ...detail.color.applied },
        segments: { [segmentId]: patch },
      };
      const result = await (detail.project_revision === undefined
        ? api.colorSettings(detail.project.id, colorPatch)
        : api.colorSettings(detail.project.id, colorPatch, detail.project_revision));
      if (!result.ok) {
        setProjectMessage(`片段調色更新失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setPreviewItems([]);
      const successMessage = !segment.colorEnabled ? "已啟用此片段調色。" : "已停用此片段調色。";
      setProjectMessage(successMessage);
      await refreshAfterMutation(successMessage);
    } catch (error) {
      setProjectMessage(`片段調色更新失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
      controls.finishProjectMutation(mutation);
    }
  }

  async function resetColor(segmentId: string) {
    if (busy) return;
    const mutation = beginMutation("color");
    if (!mutation) return;
    setBusy("color");
    try {
      const colorPatch = {
        schema_version: detail.color.schema_version,
        enabled: detail.color.enabled,
        applied: { ...detail.color.applied },
        segments: { [segmentId]: null },
      };
      const result = await (detail.project_revision === undefined
        ? api.colorSettings(detail.project.id, colorPatch)
        : api.colorSettings(detail.project.id, colorPatch, detail.project_revision));
      if (!result.ok) {
        setProjectMessage(`恢復調色預設失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setPreviewItems([]);
      const successMessage = "已恢復此片段的專案調色預設。";
      setProjectMessage(successMessage);
      await refreshAfterMutation(successMessage);
    } catch (error) {
      setProjectMessage(`恢復調色預設失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
      controls.finishProjectMutation(mutation);
    }
  }

  return <StoryboardReviewWorkspace
    model={model}
    selectedId={selectedId}
    dirty={dirty}
    busy={workspaceBusy}
    saving={busy === "save"}
    regenerating={busy === "regenerate"}
    previewing={busy === "preview"}
    thumbnailing={busy === "thumbnail"}
    timingDrafts={timingDrafts}
    timingDirty={timingDirty}
    hasUnsavedTiming={hasUnsavedTiming}
    previewItems={previewItems}
    onSelect={selectSegment}
    onStoryboardChange={changeStoryboard}
    onTimingChange={changeTiming}
    onSaveTiming={(segmentId) => void saveTiming(segmentId)}
    onResetTiming={resetTiming}
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

  async function refreshAfterMutation(successMessage: string) {
    try {
      await refreshProject({ forceFresh: true, throwOnError: true });
    } catch (error) {
      setProjectMessage(successMessage + " 但畫面更新失敗：" + errorMessage(error));
    }
  }
}

function timingFromDetail(detail: ProjectDetail): Record<string, TimingDraft> {
  return Object.fromEntries(detail.segments.map((segment) => [segment.segment_id, {
    startSeconds: segment.start_seconds,
    endSeconds: segment.end_seconds,
    speed: segment.speed || 1,
  }]));
}

function syncServerTiming(
  current: Record<string, TimingDraft>,
  detail: ProjectDetail,
  dirty: Record<string, boolean>,
  committed: Record<string, TimingDraft>,
): Record<string, TimingDraft> {
  return Object.fromEntries(detail.segments.map((segment) => {
    const server = timingForSegment(detail, segment.segment_id);
    return [segment.segment_id, dirty[segment.segment_id] && current[segment.segment_id]
      ? current[segment.segment_id]
      : committed[segment.segment_id] || server];
  }));
}

function committedTimingForSegment(
  committed: Record<string, TimingDraft>,
  detail: ProjectDetail,
  segmentId: string,
): TimingDraft {
  return committed[segmentId] || timingForSegment(detail, segmentId);
}

function timingForSegment(detail: ProjectDetail, segmentId: string): TimingDraft {
  const segment = detail.segments.find((item) => item.segment_id === segmentId);
  return {
    startSeconds: segment?.start_seconds ?? 0,
    endSeconds: segment?.end_seconds ?? 0,
    speed: segment?.speed || 1,
  };
}

function sameTiming(left: TimingDraft, right: TimingDraft): boolean {
  return Math.abs(left.startSeconds - right.startSeconds) < 0.0005
    && Math.abs(left.endSeconds - right.endSeconds) < 0.0005
    && Math.abs(left.speed - right.speed) < 0.0005;
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
  return formatApiError(error);
}
