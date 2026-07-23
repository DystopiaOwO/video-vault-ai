import type {
  AudioSegmentSettings,
  ProjectDetail,
  Segment,
  StoryboardSegment,
  StoryboardState,
} from "../../api";

export type StoryboardSegmentView = {
  id: string;
  title: string;
  sourceName: string;
  groupId: string;
  groupTitle: string;
  groupOrder: number;
  order: number;
  included: boolean;
  locked: boolean;
  thumbnailUrl: string;
  thumbnailRatio: number;
  startSeconds: number;
  endSeconds: number;
  speed: number;
  durationSeconds: number;
  score: number;
  sceneRole: string;
  storyPosition: string;
  suggestedUse: string;
  notes: string;
  audioRole: AudioSegmentSettings["role"];
  audioLabel: string;
  audioCustomized: boolean;
  colorEnabled: boolean;
  colorCustomized: boolean;
  colorWarnings: string[];
};

export type StoryboardGroupView = {
  id: string;
  title: string;
  category: string;
  order: number;
  segments: StoryboardSegmentView[];
};

export type StoryboardViewModel = {
  exists: boolean;
  valid: boolean;
  errors: string[];
  warnings: string[];
  groups: StoryboardGroupView[];
  segments: StoryboardSegmentView[];
  summary: {
    totalSegments: number;
    includedSegments: number;
    excludedSegments: number;
    estimatedDurationSeconds: number;
  };
};

export type StoryboardSegmentEdit = Pick<
  StoryboardSegment,
  "group_id" | "order" | "included" | "locked" | "thumbnail_time_ratio" | "notes" | "thumbnail_url"
>;

const AUDIO_LABELS: Record<AudioSegmentSettings["role"], string> = {
  keep: "保留原音",
  lower: "降低原音",
  mute: "靜音",
  bgm_only: "只留 BGM",
};

export function buildStoryboardViewModel(detail: ProjectDetail): StoryboardViewModel {
  const state = detail.storyboard || emptyStoryboard();
  const declared = [...(state.groups || [])].sort(compareOrder);
  const declaredById = new Map(declared.map((group) => [group.group_id, group]));
  const fallbackIds: string[] = [];

  const rows = detail.segments.map((segment) => {
    const item = state.segments?.[segment.segment_id];
    const groupId = item?.group_id || segment.storyboard_group_id || segment.group || "ungrouped";
    if (!declaredById.has(groupId) && !fallbackIds.includes(groupId)) fallbackIds.push(groupId);
    return toSegmentView(
      detail,
      segment,
      item,
      groupId,
      declaredById.get(groupId)?.title || readableGroupName(groupId),
      declaredById.get(groupId)?.order ?? Number.MAX_SAFE_INTEGER,
    );
  });

  const groups = [
    ...declared.map((group) => ({ id: group.group_id, title: group.title, category: group.category, order: group.order })),
    ...fallbackIds.map((id, index) => ({ id, title: readableGroupName(id), category: "other", order: declared.length + index + 1 })),
  ]
    .map((group) => ({
      ...group,
      segments: rows
        .filter((segment) => segment.groupId === group.id)
        .sort((left, right) => left.order - right.order || left.startSeconds - right.startSeconds || left.id.localeCompare(right.id)),
    }))
    .filter((group) => group.segments.length > 0 || declaredById.has(group.id))
    .sort(compareOrder);

  const ordered = groups.flatMap((group) => group.segments);
  const included = ordered.filter((segment) => segment.included);
  return {
    exists: Boolean(state.exists || state.groups?.length || Object.keys(state.segments || {}).length),
    valid: state.validation?.valid !== false,
    errors: [...(state.validation?.errors || [])],
    warnings: [...(state.validation?.warnings || [])],
    groups,
    segments: ordered,
    summary: {
      totalSegments: ordered.length,
      includedSegments: included.length,
      excludedSegments: ordered.length - included.length,
      estimatedDurationSeconds: included.reduce((total, segment) => total + segment.durationSeconds, 0),
    },
  };
}

export function editableStoryboardState(detail: ProjectDetail): StoryboardState {
  const source = detail.storyboard || emptyStoryboard();
  if (!source.exists && !source.groups?.length && !Object.keys(source.segments || {}).length) return emptyStoryboard();

  const next = cloneStoryboardState(source);
  const groupIds = new Set(next.groups.map((group) => group.group_id));
  for (const [index, segment] of detail.segments.entries()) {
    const groupId = next.segments[segment.segment_id]?.group_id
      || segment.storyboard_group_id
      || segment.group
      || "ungrouped";
    if (!groupIds.has(groupId)) {
      next.groups.push({ group_id: groupId, title: readableGroupName(groupId), category: "other", order: next.groups.length + 1 });
      groupIds.add(groupId);
    }
    if (!next.segments[segment.segment_id]) {
      next.segments[segment.segment_id] = {
        group_id: groupId,
        order: segment.storyboard_order ?? segment.manual_order ?? index + 1,
        included: segment.include ?? true,
        locked: segment.storyboard_locked ?? false,
        thumbnail_time_ratio: segment.thumbnail_time_ratio ?? 0.5,
        notes: segment.storyboard_notes ?? segment.user_notes ?? "",
      };
    }
  }
  return normalizeStoryboardState(next);
}

export function updateStoryboardSegment(
  state: StoryboardState,
  segmentId: string,
  patch: Partial<StoryboardSegmentEdit>,
): StoryboardState {
  const current = state.segments?.[segmentId];
  if (!current) return state;

  if (patch.group_id && patch.group_id !== current.group_id) {
    const moved = moveStoryboardSegmentToGroup(state, segmentId, patch.group_id);
    return {
      ...moved,
      segments: {
        ...moved.segments,
        [segmentId]: { ...moved.segments[segmentId], ...patch, manual_group: true, manual_order: true },
      },
    };
  }

  return {
    ...state,
    groups: state.groups.map((group) => ({ ...group })),
    segments: { ...state.segments, [segmentId]: { ...current, ...patch } },
  };
}

export function normalizeStoryboardState(state: StoryboardState): StoryboardState {
  const groups = [...(state.groups || [])]
    .sort(compareOrder)
    .map((group, index) => ({ ...group, order: index + 1 }));
  const segments = Object.fromEntries(Object.entries(state.segments || {}).map(([id, segment]) => [id, { ...segment }]));
  for (const group of groups) {
    segmentIdsForGroup({ ...state, groups, segments }, group.group_id).forEach((id, index) => {
      segments[id] = { ...segments[id], order: index + 1 };
    });
  }
  return { ...state, groups, segments };
}

export function reorderStoryboardSegments(
  state: StoryboardState,
  sourceId: string,
  targetId: string,
  position: "before" | "after",
): StoryboardState {
  if (!sourceId || !targetId || sourceId === targetId || !state.segments[sourceId] || !state.segments[targetId]) return state;
  const next = cloneStoryboardState(state);
  const sourceGroup = next.segments[sourceId].group_id;
  const targetGroup = next.segments[targetId].group_id;
  const ids = segmentIdsForGroup(next, targetGroup).filter((id) => id !== sourceId);
  const targetIndex = ids.indexOf(targetId);
  ids.splice(Math.max(0, targetIndex + (position === "after" ? 1 : 0)), 0, sourceId);
  next.segments[sourceId] = {
    ...next.segments[sourceId],
    group_id: targetGroup,
    manual_group: Boolean(next.segments[sourceId].manual_group || sourceGroup !== targetGroup),
    manual_order: true,
  };
  ids.forEach((id, index) => {
    next.segments[id] = { ...next.segments[id], order: index + 1, manual_order: true };
  });
  return normalizeStoryboardState(next);
}

export function moveStoryboardSegment(state: StoryboardState, segmentId: string, delta: number): StoryboardState {
  const current = state.segments[segmentId];
  if (!current || !delta) return state;
  const ids = segmentIdsForGroup(state, current.group_id);
  const currentIndex = ids.indexOf(segmentId);
  const targetIndex = currentIndex + delta;
  if (currentIndex < 0 || targetIndex < 0 || targetIndex >= ids.length) return state;
  return reorderStoryboardSegments(state, segmentId, ids[targetIndex], delta < 0 ? "before" : "after");
}

export function moveStoryboardSegmentToGroup(state: StoryboardState, segmentId: string, groupId: string): StoryboardState {
  if (!state.segments[segmentId] || !state.groups.some((group) => group.group_id === groupId)) return state;
  const next = cloneStoryboardState(state);
  const sourceGroup = next.segments[segmentId].group_id;
  const ids = segmentIdsForGroup(next, groupId).filter((id) => id !== segmentId);
  ids.push(segmentId);
  next.segments[segmentId] = {
    ...next.segments[segmentId],
    group_id: groupId,
    manual_group: Boolean(next.segments[segmentId].manual_group || sourceGroup !== groupId),
    manual_order: true,
  };
  ids.forEach((id, index) => {
    next.segments[id] = { ...next.segments[id], order: index + 1, manual_order: true };
  });
  return normalizeStoryboardState(next);
}

export function addStoryboardGroup(state: StoryboardState, title: string, groupId = `custom_${Date.now()}`): StoryboardState {
  const trimmed = title.trim();
  if (!trimmed || state.groups.some((group) => group.group_id === groupId)) return state;
  const next = cloneStoryboardState(state);
  next.groups.push({ group_id: groupId, title: trimmed, category: "custom", order: next.groups.length + 1 });
  return normalizeStoryboardState(next);
}

export function renameStoryboardGroup(state: StoryboardState, groupId: string, title: string): StoryboardState {
  if (!state.groups.some((group) => group.group_id === groupId)) return state;
  const next = cloneStoryboardState(state);
  next.groups = next.groups.map((group) => group.group_id === groupId ? { ...group, title } : group);
  return next;
}

export function moveStoryboardGroup(state: StoryboardState, groupId: string, delta: number): StoryboardState {
  const next = cloneStoryboardState(state);
  const groups = [...next.groups].sort(compareOrder);
  const currentIndex = groups.findIndex((group) => group.group_id === groupId);
  const targetIndex = currentIndex + delta;
  if (currentIndex < 0 || targetIndex < 0 || targetIndex >= groups.length) return state;
  [groups[currentIndex], groups[targetIndex]] = [groups[targetIndex], groups[currentIndex]];
  next.groups = groups.map((group, index) => ({ ...group, order: index + 1 }));
  return normalizeStoryboardState(next);
}

export function deleteEmptyStoryboardGroup(state: StoryboardState, groupId: string): StoryboardState {
  if (Object.values(state.segments).some((segment) => segment.group_id === groupId)) return state;
  const next = cloneStoryboardState(state);
  next.groups = next.groups.filter((group) => group.group_id !== groupId);
  return normalizeStoryboardState(next);
}

export function validateSegmentTiming(
  startSeconds: number,
  endSeconds: number,
  speed: number,
  sourceDurationSeconds?: number,
): string[] {
  const errors: string[] = [];
  if (![startSeconds, endSeconds, speed].every(Number.isFinite)) errors.push("片段時間與速度必須是有限數值");
  if (startSeconds < 0) errors.push("片段起點不可小於 0 秒");
  if (endSeconds <= startSeconds) errors.push("片段終點必須大於起點");
  if (speed < 0.25 || speed > 4) errors.push("片段速度必須介於 0.25 到 4 倍");
  if (sourceDurationSeconds !== undefined && sourceDurationSeconds > 0 && endSeconds > sourceDurationSeconds + 0.001) errors.push("片段終點超過來源影片長度");
  return errors;
}

export function timelineStartForSegment(
  state: StoryboardState,
  detail: ProjectDetail,
  segmentId: string,
  timings: Record<string, { startSeconds: number; endSeconds: number; speed: number }> = {},
): number {
  const normalized = normalizeStoryboardState(state);
  let total = 0;
  for (const group of normalized.groups) {
    for (const id of segmentIdsForGroup(normalized, group.group_id)) {
      const item = normalized.segments[id];
      if (!item?.included) continue;
      if (id === segmentId) return total;
      const segment = detail.segments.find((row) => row.segment_id === id);
      const timing = timings[id];
      const start = timing?.startSeconds ?? segment?.start_seconds ?? 0;
      const end = timing?.endSeconds ?? segment?.end_seconds ?? start;
      const speed = timing?.speed ?? segment?.speed ?? 1;
      total += Math.max(0, end - start) / Math.max(0.25, speed);
    }
  }
  return total;
}

function toSegmentView(
  detail: ProjectDetail,
  segment: Segment,
  storyboard: StoryboardSegment | undefined,
  groupId: string,
  groupTitle: string,
  groupOrder: number,
): StoryboardSegmentView {
  const audioOverride = detail.audio.segments?.[segment.segment_id];
  const audioRole = normalizeAudioRole(audioOverride?.role || storyboard?.effective_audio_role || detail.audio.original_audio.default_role || segment.audio_role);
  const colorOverride = detail.color.segments?.[segment.segment_id];
  const colorEnabled = (detail.color.enabled || colorOverride?.enabled === true)
    && colorOverride?.enabled !== false
    && colorOverride?.excluded !== true;
  const startSeconds = finiteOr(segment.start_seconds, 0);
  const endSeconds = finiteOr(segment.end_seconds, startSeconds);
  const speed = Math.min(4, Math.max(0.25, finiteOr(segment.speed, 1)));
  return {
    id: segment.segment_id,
    title: segment.title || segment.segment_id,
    sourceName: segment.source_filename || segment.clip_id || "未知素材",
    groupId,
    groupTitle,
    groupOrder,
    order: storyboard?.order ?? segment.storyboard_order ?? segment.manual_order ?? Number.MAX_SAFE_INTEGER,
    included: storyboard?.included ?? segment.include ?? true,
    locked: storyboard?.locked ?? segment.storyboard_locked ?? false,
    thumbnailUrl: storyboard?.thumbnail_url || "",
    thumbnailRatio: storyboard?.thumbnail_time_ratio ?? segment.thumbnail_time_ratio ?? 0.5,
    startSeconds,
    endSeconds,
    speed,
    durationSeconds: Math.max(0, endSeconds - startSeconds) / speed,
    score: finiteOr(segment.score, 0),
    sceneRole: segment.scene_role || "未分類",
    storyPosition: segment.story_position || "未指定",
    suggestedUse: segment.suggested_use || "未指定",
    notes: storyboard?.notes ?? segment.storyboard_notes ?? segment.user_notes ?? "",
    audioRole,
    audioLabel: AUDIO_LABELS[audioRole],
    audioCustomized: Boolean(audioOverride && typeof audioOverride === "object"),
    colorEnabled,
    colorCustomized: Boolean(colorOverride),
    colorWarnings: [...(colorOverride?.warnings || [])],
  };
}

function cloneStoryboardState(state: StoryboardState): StoryboardState {
  return {
    ...state,
    groups: (state.groups || []).map((group) => ({ ...group })),
    segments: Object.fromEntries(Object.entries(state.segments || {}).map(([id, segment]) => [id, { ...segment }])),
  };
}

function segmentIdsForGroup(state: StoryboardState, groupId: string): string[] {
  return Object.entries(state.segments || {})
    .filter(([, segment]) => segment.group_id === groupId)
    .sort(([, left], [, right]) => left.order - right.order)
    .map(([id]) => id);
}

function emptyStoryboard(): StoryboardState {
  return { schema_version: 1, exists: false, groups: [], segments: {} };
}

function compareOrder<T extends { order: number; title?: string }>(left: T, right: T): number {
  return left.order - right.order || String(left.title || "").localeCompare(String(right.title || ""));
}

function normalizeAudioRole(value: string | undefined): AudioSegmentSettings["role"] {
  if (value === "keep" || value === "keep_original") return "keep";
  if (value === "mute") return "mute";
  if (value === "bgm_only") return "bgm_only";
  return "lower";
}

function readableGroupName(value: string): string {
  if (!value || value === "ungrouped") return "未分組";
  return value.replace(/_/g, " ");
}

function finiteOr(value: number | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}
