import { useEffect, useMemo, useRef, useState } from "react";
import { isCommittedEnter } from "../../keyboard";
import type { AudioSegmentSettings } from "../../api";
import {
  type StoryboardSegmentEdit,
  type StoryboardSegmentView,
  type StoryboardViewModel,
  validateSegmentTiming,
} from "./storyboardViewModel";
import "./storyboard-review.css";

export type SegmentTimingPatch = Partial<{
  startSeconds: number;
  endSeconds: number;
  speed: number;
}>;

export type StoryboardPreviewMode = "segment" | "transition" | "range";
export type StoryboardVisibilityFilter = "all" | "included" | "excluded";

export type StoryboardPreviewItem = {
  kind: string;
  url?: string;
  durationSeconds: number;
};

export type StoryboardReviewWorkspaceProps = {
  model: StoryboardViewModel;
  selectedId?: string;
  dirty?: boolean;
  busy?: boolean;
  saving?: boolean;
  regenerating?: boolean;
  previewing?: boolean;
  thumbnailing?: boolean;
  timingDrafts?: Record<string, { startSeconds: number; endSeconds: number; speed: number }>;
  timingDirty?: Record<string, boolean>;
  hasUnsavedTiming?: boolean;
  previewItems?: StoryboardPreviewItem[];
  onSelect: (segmentId: string) => void;
  onStoryboardChange: (segmentId: string, patch: Partial<StoryboardSegmentEdit>) => void;
  onTimingChange: (segmentId: string, patch: SegmentTimingPatch) => void;
  onSaveTiming: (segmentId: string) => void;
  onResetTiming?: (segmentId: string) => void;
  onSave: () => void;
  onRegenerate: () => void;
  onPreview: (segmentId: string, mode: StoryboardPreviewMode, force?: boolean) => void;
  onAudioRoleChange?: (segmentId: string, role: AudioSegmentSettings["role"] | "default") => void;
  onToggleColor?: (segmentId: string) => void;
  onResetColor?: (segmentId: string) => void;
  onGenerateThumbnail?: (segmentId: string, ratio: number, force?: boolean) => void;
  onMoveSegment?: (segmentId: string, delta: number) => void;
  onAddGroup?: (title: string) => void;
  onRenameGroup?: (groupId: string, title: string) => void;
  onMoveGroup?: (groupId: string, delta: number) => void;
  onDeleteGroup?: (groupId: string) => void;
};

export function StoryboardReviewWorkspace({
  model,
  selectedId,
  dirty = false,
  busy = false,
  saving = false,
  regenerating = false,
  previewing = false,
  thumbnailing = false,
  timingDrafts = {},
  timingDirty = {},
  hasUnsavedTiming = false,
  previewItems = [],
  onSelect,
  onStoryboardChange,
  onTimingChange,
  onSaveTiming,
  onResetTiming,
  onSave,
  onRegenerate,
  onPreview,
  onAudioRoleChange,
  onToggleColor,
  onResetColor,
  onGenerateThumbnail,
  onMoveSegment,
  onAddGroup,
  onRenameGroup,
  onMoveGroup,
  onDeleteGroup,
}: StoryboardReviewWorkspaceProps) {
  const [newGroupTitle, setNewGroupTitle] = useState("");
  const [query, setQuery] = useState("");
  const [visibility, setVisibility] = useState<StoryboardVisibilityFilter>("all");
  const [collapsedGroupIds, setCollapsedGroupIds] = useState<string[]>([]);
  const inspectorRef = useRef<HTMLElement | null>(null);
  const previousSelectedId = useRef<string | undefined>(undefined);
  const selected = model.segments.find((segment) => segment.id === selectedId) || model.segments[0];
  const timingDirtyCount = Object.values(timingDirty).filter(Boolean).length;
  const groupReorderBlocked = query.trim().length > 0 || visibility !== "all";
  const groupReorderHint = "搜尋或篩選期間無法安全調整分組順序，請先清除篩選。";

  const visibleGroups = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    return model.groups
      .map((group) => ({
        ...group,
        segments: group.segments.filter((segment) => {
          if (visibility === "included" && !segment.included) return false;
          if (visibility === "excluded" && segment.included) return false;
          if (!normalizedQuery) return true;
          return [segment.title, segment.sourceName, segment.sceneRole, segment.storyPosition, segment.suggestedUse, segment.notes, group.title]
            .some((value) => value.toLocaleLowerCase().includes(normalizedQuery));
        }),
      }))
      .filter((group) => group.segments.length > 0 || (!normalizedQuery && visibility === "all"));
  }, [model.groups, query, visibility]);
  const visibleSegments = useMemo(
    () => visibleGroups.flatMap((group) => collapsedGroupIds.includes(group.id) ? [] : group.segments),
    [collapsedGroupIds, visibleGroups],
  );

  const selectedGroup = selected ? model.groups.find((group) => group.id === selected.groupId) : undefined;
  const selectedIndex = selectedGroup?.segments.findIndex((segment) => segment.id === selected?.id) ?? -1;

  useEffect(() => {
    const previous = previousSelectedId.current;
    previousSelectedId.current = selectedId;
    if (!previous || previous === selectedId || typeof window === "undefined" || window.innerWidth > 980) return;
    window.requestAnimationFrame(() => inspectorRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" }));
  }, [selectedId]);

  function addGroup() {
    const title = newGroupTitle.trim();
    if (!title || !onAddGroup || busy) return;
    onAddGroup(title);
    setNewGroupTitle("");
  }

  function toggleGroup(groupId: string) {
    setCollapsedGroupIds((current) => current.includes(groupId)
      ? current.filter((id) => id !== groupId)
      : [...current, groupId]);
  }

  function navigateSegment(segmentId: string, delta: number) {
    const index = visibleSegments.findIndex((segment) => segment.id === segmentId);
    const target = visibleSegments[index + delta];
    if (target) onSelect(target.id);
  }

  if (!model.exists || model.segments.length === 0) {
    return <section className="review-empty" aria-label="分鏡審核">
      <span>STORYBOARD REVIEW</span>
      <h2>尚未建立分鏡</h2>
      <p>先依內容感知結果建立分鏡，再進行片段排序、剪點與人工審核。</p>
      <button type="button" className="review-primary" disabled={busy || regenerating} onClick={onRegenerate}>
        {regenerating ? "建立中…" : "建立分鏡"}
      </button>
    </section>;
  }

  return <section className="review-workspace" aria-label="分鏡審核工作區">
    <div className="review-browser">
      <header className="review-toolbar">
        <div>
          <span>STORYBOARD REVIEW</span>
          <h2>分鏡片段</h2>
          <p>依敘事分組檢視片段；完整設定集中在右側 Inspector。</p>
        </div>
        <div className="review-toolbar-actions">
          {dirty && <strong className="review-dirty">有未儲存變更</strong>}
          {timingDirtyCount > 0 && <strong className="review-dirty timing">剪點未儲存 {timingDirtyCount}</strong>}
          <button
            type="button"
            disabled={busy || regenerating || dirty || timingDirtyCount > 0}
            title={dirty || timingDirtyCount > 0 ? "請先儲存目前變更" : undefined}
            onClick={onRegenerate}
          >{regenerating ? "重建中…" : "重新產生"}</button>
          <button type="button" className="review-primary" disabled={busy || saving || !dirty} onClick={onSave}>{saving ? "儲存中…" : "儲存分鏡"}</button>
        </div>
      </header>

      <div className="review-summary" aria-label="分鏡摘要">
        <span>共 {model.summary.totalSegments} 段</span>
        <span>納入 {model.summary.includedSegments} 段</span>
        <span>排除 {model.summary.excludedSegments} 段</span>
        <span>預估 {formatDuration(model.summary.estimatedDurationSeconds)}</span>
      </div>

      {!model.valid && <div className="review-validation error"><b>分鏡尚未通過檢查</b>{model.errors.map((error) => <span key={error}>{error}</span>)}</div>}
      {model.warnings.length > 0 && <div className="review-validation warning">{model.warnings.map((warning) => <span key={warning}>{warning}</span>)}</div>}

      <div className="review-list-controls" aria-label="片段清單工具">
        <label className="review-search">
          <span>搜尋片段</span>
          <input aria-label="搜尋片段" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="標題、素材、場景角色或備註" />
        </label>
        <div className="review-filter" aria-label="片段納入狀態">
          {(["all", "included", "excluded"] as StoryboardVisibilityFilter[]).map((value) => <button
            key={value}
            type="button"
            aria-pressed={visibility === value}
            onClick={() => setVisibility(value)}
          >{visibilityLabel(value)}</button>)}
        </div>
        <div className="review-list-meta">
          <span>顯示 {visibleGroups.reduce((total, group) => total + group.segments.length, 0)} / {model.summary.totalSegments}</span>
          <button
            type="button"
            onClick={() => setCollapsedGroupIds(collapsedGroupIds.length ? [] : model.groups.map((group) => group.id))}
          >{collapsedGroupIds.length ? "全部展開" : "全部收合"}</button>
        </div>
        {groupReorderBlocked && <span className="review-filter-hint" role="status">{groupReorderHint}</span>}
      </div>

      {onAddGroup && <div className="review-add-group">
        <input aria-label="新增分組名稱" disabled={busy} value={newGroupTitle} onChange={(event) => setNewGroupTitle(event.target.value)} placeholder="新增自訂分組" onKeyDown={(event) => { if (isCommittedEnter(event)) { event.preventDefault(); addGroup(); } }} />
        <button type="button" disabled={busy || !newGroupTitle.trim()} onClick={addGroup}>新增分組</button>
      </div>}

      <div className="review-groups">
        {visibleGroups.map((group) => {
          const collapsed = collapsedGroupIds.includes(group.id);
          const fullGroupIndex = model.groups.findIndex((candidate) => candidate.id === group.id);
          return <section className="review-group" key={group.id}>
            <header>
              <button type="button" className="review-group-toggle" aria-expanded={!collapsed} aria-label={`${group.title} ${collapsed ? "展開" : "收合"}`} onClick={() => toggleGroup(group.id)}>
                <span aria-hidden="true">{collapsed ? "▸" : "▾"}</span>
              </button>
              <div className="review-group-copy">
                {onRenameGroup
                  ? <input aria-label={`${group.title} 分組名稱`} disabled={busy} value={group.title} onChange={(event) => onRenameGroup(group.id, event.target.value)} />
                  : <b>{group.title}</b>}
                <small>{group.segments.length} 個片段 · {formatDuration(group.segments.filter((segment) => segment.included).reduce((total, segment) => total + segment.durationSeconds, 0))}</small>
              </div>
              <div className="review-group-actions">
                <span>{group.category}</span>
                {onMoveGroup && <button type="button" aria-label={`${group.title} 分組上移`} title={groupReorderBlocked ? groupReorderHint : undefined} disabled={busy || groupReorderBlocked || fullGroupIndex <= 0} onClick={() => onMoveGroup(group.id, -1)}>↑</button>}
                {onMoveGroup && <button type="button" aria-label={`${group.title} 分組下移`} title={groupReorderBlocked ? groupReorderHint : undefined} disabled={busy || groupReorderBlocked || fullGroupIndex < 0 || fullGroupIndex >= model.groups.length - 1} onClick={() => onMoveGroup(group.id, 1)}>↓</button>}
                {onDeleteGroup && group.segments.length === 0 && <button type="button" disabled={busy} onClick={() => onDeleteGroup(group.id)}>刪除</button>}
              </div>
            </header>
            {!collapsed && <div className="review-segment-list">
              {group.segments.map((segment, index) => <SegmentRow
                key={segment.id}
                segment={segment}
                index={index}
                selected={selected?.id === segment.id}
                disabled={busy}
                onSelect={onSelect}
                onNavigate={navigateSegment}
              />)}
            </div>}
          </section>;
        })}
        {visibleGroups.length === 0 && <div className="review-no-results">
          <b>找不到符合條件的片段</b>
          <span>調整搜尋字詞或切換納入狀態。</span>
          <button type="button" onClick={() => { setQuery(""); setVisibility("all"); }}>清除篩選</button>
        </div>}
      </div>
    </div>

    {selected && <SegmentInspector
      asideRef={inspectorRef}
      segment={selected}
      groups={model.groups.map((group) => ({ id: group.id, title: group.title }))}
      timingDraft={timingDrafts[selected.id]}
      timingDirty={Boolean(timingDirty[selected.id])}
      hasUnsavedTiming={hasUnsavedTiming}
      busy={busy}
      previewing={previewing}
      thumbnailing={thumbnailing}
      previewItems={previewItems}
      canMoveUp={selectedIndex > 0}
      canMoveDown={Boolean(selectedGroup && selectedIndex >= 0 && selectedIndex < selectedGroup.segments.length - 1)}
      onStoryboardChange={onStoryboardChange}
      onTimingChange={onTimingChange}
      onSaveTiming={onSaveTiming}
      onResetTiming={onResetTiming}
      onPreview={onPreview}
      onAudioRoleChange={onAudioRoleChange}
      onToggleColor={onToggleColor}
      onResetColor={onResetColor}
      onGenerateThumbnail={onGenerateThumbnail}
      onMoveSegment={onMoveSegment}
    />}
  </section>;
}

function SegmentRow({ segment, index, selected, disabled, onSelect, onNavigate }: {
  segment: StoryboardSegmentView;
  index: number;
  selected: boolean;
  disabled: boolean;
  onSelect: (segmentId: string) => void;
  onNavigate: (segmentId: string, delta: number) => void;
}) {
  return <button
    type="button"
    className={`review-segment${selected ? " selected" : ""}${segment.included ? "" : " excluded"}`}
    aria-pressed={selected}
    disabled={disabled}
    title={`${segment.title} · ${segment.sourceName}`}
    onClick={() => onSelect(segment.id)}
    onKeyDown={(event) => {
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault();
        onNavigate(segment.id, event.key === "ArrowUp" ? -1 : 1);
      }
    }}
  >
    <span className="review-order" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
    <span className="review-thumbnail">
      {segment.thumbnailUrl ? <img src={segment.thumbnailUrl} alt="" /> : <i>{String(index + 1).padStart(2, "0")}</i>}
    </span>
    <span className="review-segment-copy">
      <b>{segment.title}</b>
      <small title={segment.sourceName}>{segment.sourceName}</small>
      <span>{segment.startSeconds.toFixed(1)}–{segment.endSeconds.toFixed(1)} 秒 · 成片 {segment.durationSeconds.toFixed(1)} 秒 · AI {segment.score.toFixed(2)}</span>
    </span>
    <span className="review-tags">
      <i>{segment.sceneRole}</i>
      <i className={segment.included ? "success" : "muted"}>{segment.included ? "已納入" : "已排除"}</i>
      {segment.locked && <i>已鎖定</i>}
    </span>
  </button>;
}

function SegmentInspector({
  asideRef,
  segment,
  groups,
  timingDraft,
  timingDirty,
  hasUnsavedTiming,
  busy,
  previewing,
  thumbnailing,
  previewItems,
  canMoveUp,
  canMoveDown,
  onStoryboardChange,
  onTimingChange,
  onSaveTiming,
  onResetTiming,
  onPreview,
  onAudioRoleChange,
  onToggleColor,
  onResetColor,
  onGenerateThumbnail,
  onMoveSegment,
}: {
  asideRef: React.RefObject<HTMLElement | null>;
  segment: StoryboardSegmentView;
  groups: Array<{ id: string; title: string }>;
  timingDraft?: { startSeconds: number; endSeconds: number; speed: number };
  timingDirty: boolean;
  hasUnsavedTiming: boolean;
  busy: boolean;
  previewing: boolean;
  thumbnailing: boolean;
  previewItems: StoryboardPreviewItem[];
  canMoveUp: boolean;
  canMoveDown: boolean;
  onStoryboardChange: (segmentId: string, patch: Partial<StoryboardSegmentEdit>) => void;
  onTimingChange: (segmentId: string, patch: SegmentTimingPatch) => void;
  onSaveTiming: (segmentId: string) => void;
  onResetTiming?: (segmentId: string) => void;
  onPreview: (segmentId: string, mode: StoryboardPreviewMode, force?: boolean) => void;
  onAudioRoleChange?: (segmentId: string, role: AudioSegmentSettings["role"] | "default") => void;
  onToggleColor?: (segmentId: string) => void;
  onResetColor?: (segmentId: string) => void;
  onGenerateThumbnail?: (segmentId: string, ratio: number, force?: boolean) => void;
  onMoveSegment?: (segmentId: string, delta: number) => void;
}) {
  const [ignoreCache, setIgnoreCache] = useState(false);
  const timing = timingDraft || {
    startSeconds: segment.startSeconds,
    endSeconds: segment.endSeconds,
    speed: segment.speed,
  };
  const timingErrors = validateSegmentTiming(timing.startSeconds, timing.endSeconds, timing.speed);
  const outputDuration = Math.max(0, timing.endSeconds - timing.startSeconds) / Math.max(0.25, timing.speed);
  const previewBlocked = timingDirty || hasUnsavedTiming || !segment.included;
  const previewHint = !segment.included
    ? "此片段已排除，不會進入正式輸出；請先納入成片後再預覽。"
    : timingDirty || hasUnsavedTiming
      ? "請先儲存所有未完成的片段剪點，再產生預覽。"
      : "先看短預覽，再決定是否正式輸出";

  return <aside ref={asideRef} className="review-inspector" aria-label="片段設定">
    <header>
      <div><span>SEGMENT INSPECTOR</span><h2>片段設定</h2></div>
      {timingDirty && <strong className="review-dirty timing">剪點未儲存</strong>}
    </header>
    <div className="review-inspector-preview">
      {segment.thumbnailUrl ? <img src={segment.thumbnailUrl} alt={`${segment.title} 代表畫格`} /> : <span>尚未產生代表畫格</span>}
      <div><b>{segment.title}</b><small title={segment.sourceName}>{segment.sourceName}</small></div>
    </div>

    <section className="review-inspector-section">
      <div className="review-section-title"><b>分鏡與排序</b><span>控制是否納入成片與敘事位置</span></div>
      <div className="review-toggle-grid">
        <label><input type="checkbox" disabled={busy} checked={segment.included} onChange={(event) => onStoryboardChange(segment.id, { included: event.target.checked })} />納入成片</label>
        <label><input type="checkbox" disabled={busy} checked={segment.locked} onChange={(event) => onStoryboardChange(segment.id, { locked: event.target.checked })} />鎖定片段</label>
      </div>

      {onMoveSegment && <div className="review-order-actions">
        <button type="button" disabled={busy || !canMoveUp} onClick={() => onMoveSegment(segment.id, -1)}>片段上移</button>
        <button type="button" disabled={busy || !canMoveDown} onClick={() => onMoveSegment(segment.id, 1)}>片段下移</button>
      </div>}

      <div className="review-form-grid">
        <label className="wide">分組<select aria-label="片段分組" disabled={busy} value={segment.groupId} onChange={(event) => onStoryboardChange(segment.id, { group_id: event.target.value })}>{groups.map((group) => <option key={group.id} value={group.id}>{group.title}</option>)}</select></label>
        <label className="wide">分鏡備註<textarea aria-label="分鏡備註" disabled={busy} value={segment.notes} onChange={(event) => onStoryboardChange(segment.id, { notes: event.target.value })} placeholder="記錄保留原因、節奏或畫面用途" /></label>
      </div>
    </section>

    <section className="review-inspector-section">
      <div className="review-section-title"><b>剪點與速度</b><span>剪點需獨立儲存，避免誤套用到其他分鏡設定</span></div>
      <div className="review-form-grid">
        <label>起點（秒）<input aria-label="片段起點" disabled={busy} type="number" min={0} step={0.001} value={timing.startSeconds} onChange={(event) => onTimingChange(segment.id, { startSeconds: Number(event.target.value) })} /></label>
        <label>終點（秒）<input aria-label="片段終點" disabled={busy} type="number" min={0} step={0.001} value={timing.endSeconds} onChange={(event) => onTimingChange(segment.id, { endSeconds: Number(event.target.value) })} /></label>
        <label>速度<select aria-label="片段速度" disabled={busy} value={timing.speed} onChange={(event) => onTimingChange(segment.id, { speed: Number(event.target.value) })}><option value={0.5}>0.50×</option><option value={0.75}>0.75×</option><option value={1}>1.00×</option><option value={1.15}>1.15×</option><option value={1.25}>1.25×</option><option value={1.5}>1.50×</option><option value={2}>2.00×</option></select></label>
        <label>成片長度<input aria-label="成片長度" readOnly value={`${outputDuration.toFixed(1)} 秒`} /></label>
      </div>
      {timingErrors.length > 0 && <div className="review-timing-errors" role="alert">{timingErrors.map((error) => <span key={error}>{error}</span>)}</div>}
      <div className="review-timing-actions">
        <button type="button" disabled={busy || !timingDirty || !onResetTiming} onClick={() => onResetTiming?.(segment.id)}>放棄剪點變更</button>
        <button type="button" className="review-primary" disabled={busy || !timingDirty || timingErrors.length > 0} onClick={() => onSaveTiming(segment.id)}>儲存剪點</button>
      </div>
    </section>

    <section className="review-inspector-section">
      <div className="review-section-title"><b>畫格、音訊與調色</b><span>片段自訂會覆蓋專案預設</span></div>
      <div className="review-form-grid">
        <label className="wide">代表畫格<select aria-label="代表畫格位置" disabled={busy} value={segment.thumbnailRatio} onChange={(event) => onStoryboardChange(segment.id, { thumbnail_time_ratio: Number(event.target.value) })}><option value={0.25}>片段 25%</option><option value={0.5}>片段 50%</option><option value={0.75}>片段 75%</option></select></label>
        {onGenerateThumbnail && <button type="button" className="review-wide-button" disabled={busy || thumbnailing} onClick={() => onGenerateThumbnail(segment.id, segment.thumbnailRatio, ignoreCache)}>{thumbnailing ? "產生中…" : "產生代表畫格"}</button>}
        <label className="wide review-force-toggle"><input aria-label="忽略快取並強制重跑" type="checkbox" disabled={busy} checked={ignoreCache} onChange={(event) => setIgnoreCache(event.target.checked)} />忽略快取並強制重跑</label>
        <label className="wide">原音角色<select aria-label="原音角色" value={segment.audioCustomized ? segment.audioRole : "default"} disabled={busy || !onAudioRoleChange} onChange={(event) => onAudioRoleChange?.(segment.id, event.target.value as AudioSegmentSettings["role"] | "default")}><option value="default">使用專案預設（{segment.audioLabel}）</option><option value="keep">保留原音</option><option value="lower">降低原音</option><option value="mute">靜音</option><option value="bgm_only">只留 BGM</option></select></label>
      </div>

      <div className="review-effective-state">
        <div><span>調色</span><b>{segment.colorEnabled ? "啟用" : "停用"}{segment.colorCustomized ? " · 片段自訂" : " · 專案預設"}</b></div>
        <div className="review-effective-actions">
          <button type="button" disabled={busy || !onToggleColor} onClick={() => onToggleColor?.(segment.id)}>{segment.colorEnabled ? "停用此片段" : "啟用此片段"}</button>
          {segment.colorCustomized && onResetColor && <button type="button" disabled={busy} onClick={() => onResetColor(segment.id)}>恢復專案預設</button>}
        </div>
        {segment.colorWarnings.map((warning) => <small key={warning}>{warning}</small>)}
      </div>
    </section>

    <section className="review-inspector-section">
      <div className="review-section-title"><b>預覽</b><span>{previewHint}</span></div>
      <div className="review-preview-actions">
        <button type="button" disabled={busy || previewing || previewBlocked} onClick={() => onPreview(segment.id, "transition", ignoreCache)}>預覽前後銜接</button>
        <button type="button" disabled={busy || previewing || previewBlocked} onClick={() => onPreview(segment.id, "range", ignoreCache)}>從此片段預覽 8 秒</button>
        <button type="button" className="review-primary" disabled={busy || previewing || previewBlocked} onClick={() => onPreview(segment.id, "segment", ignoreCache)}>{previewing ? "產生中…" : "產生 5 秒預覽"}</button>
      </div>

      {previewItems.length > 0 && <div className="review-preview-results" aria-label="分鏡預覽結果">
        {previewItems.map((item, index) => <div key={`${item.kind}-${index}`}>
          <b>{previewLabel(item.kind)} · {item.durationSeconds.toFixed(1)} 秒</b>
          {item.url ? <video controls src={item.url} /> : <span>預覽檔案未提供網址</span>}
        </div>)}
      </div>}
    </section>
  </aside>;
}

function visibilityLabel(value: StoryboardVisibilityFilter): string {
  if (value === "included") return "已納入";
  if (value === "excluded") return "已排除";
  return "全部";
}

function previewLabel(kind: string): string {
  if (kind === "incoming") return "前段銜接";
  if (kind === "outgoing") return "後段銜接";
  if (kind === "range") return "分鏡範圍";
  return "片段預覽";
}

function formatDuration(value: number): string {
  const seconds = Math.max(0, Math.round(value));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
