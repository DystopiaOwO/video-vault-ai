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
  type StoryboardPreviewMode,
} from "./StoryboardReviewWorkspace";
import {
  buildStoryboardViewModel,
  updateStoryboardSegment,
  type StoryboardSegmentEdit,
} from "./storyboardViewModel";

export type StoryboardWorkspaceControllerProps = {
  detail: ProjectDetail;
  setMessage: (message: string) => void;
  refreshProject: (options?: { forceFresh?: boolean; jobs?: boolean }) => Promise<unknown>;
};

type TimingDraft = { startSeconds: number; endSeconds: number; speed: number };

export function StoryboardWorkspaceController({
  detail,
  setMessage,
  refreshProject,
}: StoryboardWorkspaceControllerProps) {
  const [state, setState] = useState<StoryboardState>(() => detail.storyboard);
  const [selectedId, setSelectedId] = useState(() => firstSegmentId(detail));
  const [timingDrafts, setTimingDrafts] = useState<Record<string, TimingDraft>>(() => timingFromDetail(detail));
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<"" | "save" | "regenerate" | "timing" | "preview" | "audio" | "color">("");
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
      setState(detail.storyboard);
      setTimingDrafts(timingFromDetail(detail));
      setSelectedId(firstSegmentId(detail));
      setBusy("");
      return;
    }

    if (!dirtyRef.current) setState(detail.storyboard);
    setTimingDrafts((current) => mergeServerTiming(current, detail));
    setSelectedId((current) => current && detail.segments.some((segment) => segment.segment_id === current)
      ? current
      : firstSegmentId(detail));
  }, [detail]);

  const model = useMemo(
    () => buildStoryboardViewModel({ ...detail, storyboard: state }),
    [detail, state],
  );

  function changeStoryboard(segmentId: string, patch: Partial<StoryboardSegmentEdit>) {
    setState((current) => updateStoryboardSegment(current, segmentId, patch));
    setDirty(true);
  }

  function changeTiming(segmentId: string, patch: SegmentTimingPatch) {
    setTimingDrafts((current) => {
      const source = current[segmentId] || timingForSegment(detail, segmentId);
      return { ...current, [segmentId]: { ...source, ...patch } };
    });
  }

  async function saveStoryboard() {
    if (!dirty || busy) return;
    setBusy("save");
    setMessage("正在儲存分鏡…");
    try {
      const result = await api.updateStoryboard(detail.project.id, state);
      if (!result.ok || !result.storyboard) {
        setMessage(`分鏡儲存失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setState(result.storyboard);
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
    setMessage("正在重新產生分鏡…");
    try {
      const result = await api.generateStoryboard(detail.project.id, true);
      if (!result.ok || !result.storyboard) {
        setMessage(`分鏡產生失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setState(result.storyboard);
      setTimingDrafts(timingFromDetail(detail));
      setSelectedId(firstStoryboardSegmentId(result.storyboard) || firstSegmentId(detail));
      setMessage("分鏡已重新產生，請確認後儲存。");
      setDirty(true);
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
      setMessage("片段剪點已儲存，輸出內容有變更，請重新核准。 ");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`剪點儲存失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
    }
  }

  async function preview(segmentId: string, mode: StoryboardPreviewMode) {
    if (busy) return;
    setBusy("preview");
    setMessage(mode === "segment" ? "正在產生片段短預覽…" : "正在產生片段銜接預覽…");
    try {
      const result = await api.storyboardPreview(detail.project.id, {
        mode: mode === "segment" ? "segment" : "transition",
        segmentId,
        durationSeconds: 5,
        storyboardState: state,
      });
      if (!result.ok) {
        setMessage(`預覽產生失敗：${result.error || "未知錯誤"}`);
        return;
      }
      const previewCount = result.previews?.length || (result.url ? 1 : 0);
      setMessage(`${mode === "segment" ? "片段" : "銜接"}預覽已完成，共 ${previewCount} 個預覽。`);
    } catch (error) {
      setMessage(`預覽產生失敗：${errorMessage(error)}`);
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
      setMessage(role === "default" ? "片段已改回專案音訊預設。" : "片段原音角色已更新，請重新確認預覽。 ");
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

  return <StoryboardReviewWorkspace
    model={model}
    selectedId={selectedId}
    dirty={dirty}
    saving={busy === "save"}
    regenerating={busy === "regenerate"}
    previewing={busy === "preview"}
    timingDrafts={timingDrafts}
    onSelect={setSelectedId}
    onStoryboardChange={changeStoryboard}
    onTimingChange={changeTiming}
    onSaveTiming={(segmentId) => void saveTiming(segmentId)}
    onSave={() => void saveStoryboard()}
    onRegenerate={() => void regenerateStoryboard()}
    onPreview={(segmentId, mode) => void preview(segmentId, mode)}
    onAudioRoleChange={(segmentId, role) => void changeAudioRole(segmentId, role)}
    onToggleColor={(segmentId) => void toggleColor(segmentId)}
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
    startSeconds: segment?.start_seconds || 0,
    endSeconds: segment?.end_seconds || 0,
    speed: segment?.speed || 1,
  };
}

function firstSegmentId(detail: ProjectDetail): string {
  return firstStoryboardSegmentId(detail.storyboard) || detail.segments[0]?.segment_id || "";
}

function firstStoryboardSegmentId(state: StoryboardState): string {
  return Object.entries(state.segments || {})
    .sort(([, left], [, right]) => left.order - right.order)
    .map(([segmentId]) => segmentId)[0] || "";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "網路或服務錯誤";
}
