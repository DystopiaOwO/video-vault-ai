import { useMemo, useState } from "react";
import "./ui-prototype-react.css";

type Workspace = "dashboard" | "storyboard" | "tuning" | "output";
type PreviewMode = "before" | "after" | "segment";
type TuningTab = "color" | "audio";
type AudioRole = "keep" | "lower" | "mute" | "bgm_only";

type Segment = {
  id: string;
  title: string;
  source: string;
  group: string;
  start: number;
  end: number;
  speed: number;
  score: number;
  sceneRole: string;
  included: boolean;
  locked: boolean;
  notes: string;
  audioRole: AudioRole;
  colorEnabled: boolean;
};

const INITIAL_SEGMENTS: Segment[] = [
  {
    id: "seg-arrival",
    title: "抵達博多站",
    source: "DJI_20260718_090112_001_D.MP4",
    group: "早上 · 抵達",
    start: 3.2,
    end: 10.8,
    speed: 1,
    score: 0.93,
    sceneRole: "opening",
    included: true,
    locked: true,
    notes: "保留車站環境音，作為旅程開始。",
    audioRole: "keep",
    colorEnabled: true,
  },
  {
    id: "seg-walk",
    title: "巷弄散步與街景",
    source: "DJI_20260718_105804_014_D.MP4",
    group: "上午 · 散步",
    start: 12.4,
    end: 21.1,
    speed: 1.15,
    score: 0.88,
    sceneRole: "journey",
    included: true,
    locked: false,
    notes: "避免連續太多走路鏡頭，節奏稍快。",
    audioRole: "lower",
    colorEnabled: true,
  },
  {
    id: "seg-coffee",
    title: "咖啡店手沖細節",
    source: "DJI_20260718_143211_027_D.MP4",
    group: "下午 · 咖啡",
    start: 5.6,
    end: 15.3,
    speed: 1,
    score: 0.96,
    sceneRole: "detail",
    included: true,
    locked: true,
    notes: "保留注水與蒸氣聲，BGM 降低。",
    audioRole: "keep",
    colorEnabled: true,
  },
  {
    id: "seg-night",
    title: "重複的夜景移動畫面",
    source: "DJI_20260718_201920_043_D.MP4",
    group: "晚上 · 夜景",
    start: 1.1,
    end: 8.4,
    speed: 1.25,
    score: 0.61,
    sceneRole: "transition",
    included: false,
    locked: false,
    notes: "與前一段構圖重複，暫時排除。",
    audioRole: "mute",
    colorEnabled: false,
  },
];

const WORKSPACES: Array<{ id: Workspace; index: string; label: string; hint: string }> = [
  { id: "dashboard", index: "01", label: "儀表板", hint: "專案總覽" },
  { id: "storyboard", index: "02", label: "分鏡審核", hint: "排序與剪點" },
  { id: "tuning", index: "03", label: "調色與音訊", hint: "一致性調整" },
  { id: "output", index: "04", label: "核准與輸出", hint: "Approval gate" },
];

function formatDuration(value: number) {
  const seconds = Math.max(0, Math.round(value));
  const minute = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minute}:${String(remainder).padStart(2, "0")}`;
}

function roleLabel(role: AudioRole) {
  return ({ keep: "保留原音", lower: "降低原音", mute: "靜音", bgm_only: "只留 BGM" } as const)[role];
}

export function UiPrototypeApp() {
  const [workspace, setWorkspace] = useState<Workspace>("dashboard");
  const [segments, setSegments] = useState<Segment[]>(INITIAL_SEGMENTS);
  const [selectedId, setSelectedId] = useState(INITIAL_SEGMENTS[0].id);
  const [previewMode, setPreviewMode] = useState<PreviewMode>("after");
  const [tuningTab, setTuningTab] = useState<TuningTab>("color");
  const [confirmStoryboard, setConfirmStoryboard] = useState(false);
  const [confirmRights, setConfirmRights] = useState(false);
  const [approved, setApproved] = useState(false);
  const [toast, setToast] = useState("這是安全的 React UI 測試頁，不會呼叫正式 API。");

  const selected = segments.find((segment) => segment.id === selectedId) ?? segments[0];
  const included = segments.filter((segment) => segment.included);
  const estimatedDuration = included.reduce((total, segment) => total + (segment.end - segment.start) / Math.max(0.25, segment.speed), 0);
  const approvalReady = confirmStoryboard && confirmRights;
  const groups = useMemo(() => Array.from(new Set(segments.map((segment) => segment.group))), [segments]);

  function patchSegment(id: string, patch: Partial<Segment>) {
    setSegments((current) => current.map((segment) => segment.id === id ? { ...segment, ...patch } : segment));
    setApproved(false);
  }

  function approve() {
    if (!approvalReady) {
      setToast("請先完成兩項人工確認。 ");
      return;
    }
    setApproved(true);
    setToast("原型狀態已核准，正式輸出按鈕已解鎖。 ");
  }

  return (
    <main className="ux-app">
      <aside className="ux-sidebar">
        <div className="ux-brand">
          <span className="ux-logo">V</span>
          <span><strong>Video Vault AI</strong><small>Local creative workspace</small></span>
        </div>
        <nav className="ux-nav" aria-label="原型工作區">
          {WORKSPACES.map((item) => (
            <button
              type="button"
              key={item.id}
              className={workspace === item.id ? "active" : ""}
              aria-current={workspace === item.id ? "page" : undefined}
              onClick={() => setWorkspace(item.id)}
            >
              <span className="ux-nav-index">{item.index}</span>
              <span><b>{item.label}</b><small>{item.hint}</small></span>
            </button>
          ))}
        </nav>
        <section className="ux-project-switcher" aria-label="目前測試專案">
          <span className="ux-section-label">目前專案</span>
          <button type="button">
            <span className="ux-project-mark" />
            <span><b>福岡旅行日記</b><small>待審核 · 8 支素材</small></span>
          </button>
        </section>
        <div className="ux-sidebar-note">
          <b>React Prototype</b>
          <p>使用示範資料驗證操作密度、資訊層級與響應式排版。</p>
        </div>
      </aside>

      <section className="ux-main">
        <header className="ux-topbar">
          <div>
            <span className="ux-eyebrow">PROJECT WORKSPACE</span>
            <strong>福岡旅行日記</strong>
          </div>
          <div className="ux-topbar-actions">
            <span className={`ux-status ${approved ? "success" : "warning"}`}>{approved ? "已核准" : "待人工審核"}</span>
            <button type="button" onClick={() => setToast("原型已回復最新示範狀態。")}>重新整理</button>
          </div>
        </header>

        <div className="ux-content">
          <div className="ux-toast" role="status"><span>{toast}</span><button type="button" aria-label="關閉通知" onClick={() => setToast("")}>×</button></div>
          <Workflow workspace={workspace} approved={approved} onNavigate={setWorkspace} />

          {workspace === "dashboard" && (
            <Dashboard
              segments={segments}
              includedCount={included.length}
              estimatedDuration={estimatedDuration}
              approved={approved}
              onNavigate={setWorkspace}
            />
          )}

          {workspace === "storyboard" && (
            <StoryboardWorkspace
              segments={segments}
              selected={selected}
              groups={groups}
              onSelect={setSelectedId}
              onPatch={patchSegment}
              onSave={() => setToast("分鏡原型狀態已儲存於瀏覽器記憶體。")}
            />
          )}

          {workspace === "tuning" && (
            <TuningWorkspace
              segments={segments}
              selected={selected}
              previewMode={previewMode}
              tuningTab={tuningTab}
              onSelect={setSelectedId}
              onPreviewMode={setPreviewMode}
              onTuningTab={setTuningTab}
              onPatch={patchSegment}
              onNotify={setToast}
            />
          )}

          {workspace === "output" && (
            <OutputWorkspace
              segments={segments}
              includedCount={included.length}
              estimatedDuration={estimatedDuration}
              confirmStoryboard={confirmStoryboard}
              confirmRights={confirmRights}
              approved={approved}
              onConfirmStoryboard={setConfirmStoryboard}
              onConfirmRights={setConfirmRights}
              onApprove={approve}
              onRender={() => setToast("正式輸出僅為原型展示，未建立 Render Job。")}
            />
          )}
        </div>
      </section>
    </main>
  );
}

function Workflow({ workspace, approved, onNavigate }: { workspace: Workspace; approved: boolean; onNavigate: (workspace: Workspace) => void }) {
  const stages: Array<{ id: Workspace; label: string; state: "done" | "current" | "pending" }> = [
    { id: "dashboard", label: "素材與故事", state: "done" },
    { id: "storyboard", label: "分鏡審核", state: workspace === "storyboard" ? "current" : "done" },
    { id: "tuning", label: "調色與音訊", state: workspace === "tuning" ? "current" : workspace === "output" ? "done" : "pending" },
    { id: "output", label: approved ? "已核准" : "核准與輸出", state: workspace === "output" ? "current" : approved ? "done" : "pending" },
  ];
  return <nav className="ux-workflow" aria-label="工作流程">
    {stages.map((stage, index) => <button type="button" key={stage.id} className={stage.state} onClick={() => onNavigate(stage.id)}><span>{stage.state === "done" ? "✓" : index + 1}</span><b>{stage.label}</b></button>)}
  </nav>;
}

function Dashboard({ segments, includedCount, estimatedDuration, approved, onNavigate }: {
  segments: Segment[];
  includedCount: number;
  estimatedDuration: number;
  approved: boolean;
  onNavigate: (workspace: Workspace) => void;
}) {
  return <div className="ux-stack">
    <section className="ux-metrics">
      <Metric label="素材數量" value="8" meta="支影片" />
      <Metric label="已選片段" value={String(includedCount)} meta={`/ ${segments.length} 段`} />
      <Metric label="預估成片" value={formatDuration(estimatedDuration)} meta="依目前剪點" />
      <Metric label="輸出狀態" value={approved ? "可輸出" : "待核准"} meta={approved ? "Gate 已通過" : "需要人工確認"} tone={approved ? "success" : "warning"} />
    </section>
    <section className="ux-dashboard-grid">
      <article className="ux-card ux-card-span-2">
        <CardHeader eyebrow="PROJECT SUMMARY" title="目前剪輯狀態" action={<button type="button" onClick={() => onNavigate("storyboard")}>進入分鏡審核</button>} />
        <div className="ux-summary-hero">
          <div><span>專案敘事</span><h2>從抵達、散步到咖啡與夜景的旅行日記</h2><p>以生活感與環境聲為主，不做教學式旁白，避免過度快速的 Highlight 節奏。</p></div>
          <div className="ux-summary-score"><strong>{Math.round((includedCount / segments.length) * 100)}</strong><small>% 片段採用率</small></div>
        </div>
        <div className="ux-thumbnail-strip" aria-label="最近分鏡預覽">{segments.map((segment, index) => <button type="button" key={segment.id} onClick={() => onNavigate("storyboard")} className={segment.included ? "" : "excluded"}><span>0{index + 1}</span><small>{segment.title}</small></button>)}</div>
      </article>
      <article className="ux-card">
        <CardHeader eyebrow="NEXT ACTION" title="建議下一步" />
        <ol className="ux-action-list">
          <li className="done"><span>1</span><div><b>內容感知完成</b><small>8 支素材已建立描述</small></div></li>
          <li><span>2</span><div><b>確認分鏡與剪點</b><small>目前有 {segments.filter((segment) => !segment.locked).length} 段未鎖定</small></div></li>
          <li><span>3</span><div><b>短預覽後核准</b><small>正式輸出前進行最後確認</small></div></li>
        </ol>
      </article>
      <article className="ux-card">
        <CardHeader eyebrow="STATUS" title="專案檢查" />
        <div className="ux-check-list">
          <StatusRow label="內容感知" value="已完成" state="success" />
          <StatusRow label="分鏡覆蓋" value={`${includedCount}/${segments.length}`} state="success" />
          <StatusRow label="調色設定" value="3 段啟用" state="success" />
          <StatusRow label="人工核准" value={approved ? "已通過" : "待處理"} state={approved ? "success" : "warning"} />
        </div>
      </article>
      <article className="ux-card">
        <CardHeader eyebrow="QUICK ACCESS" title="工作區" />
        <div className="ux-quick-grid">
          <button type="button" onClick={() => onNavigate("storyboard")}><b>分鏡審核</b><small>排序、剪點、備註</small></button>
          <button type="button" onClick={() => onNavigate("tuning")}><b>調色與音訊</b><small>預覽與片段覆寫</small></button>
          <button type="button" onClick={() => onNavigate("output")}><b>核准與輸出</b><small>檢查 Gate 狀態</small></button>
        </div>
      </article>
    </section>
  </div>;
}

function StoryboardWorkspace({ segments, selected, groups, onSelect, onPatch, onSave }: {
  segments: Segment[];
  selected: Segment;
  groups: string[];
  onSelect: (id: string) => void;
  onPatch: (id: string, patch: Partial<Segment>) => void;
  onSave: () => void;
}) {
  return <section className="ux-workspace-grid">
    <div className="ux-card ux-segment-browser">
      <CardHeader eyebrow="STORYBOARD REVIEW" title="分鏡片段" action={<div className="ux-inline-actions"><button type="button">重新產生</button><button type="button" className="primary" onClick={onSave}>儲存分鏡</button></div>} />
      <div className="ux-browser-summary"><span>共 {segments.length} 段</span><span>納入 {segments.filter((segment) => segment.included).length} 段</span><span>排除 {segments.filter((segment) => !segment.included).length} 段</span></div>
      <div className="ux-segment-list">
        {groups.map((group) => <section key={group} className="ux-segment-group">
          <header><div><b>{group}</b><small>{segments.filter((segment) => segment.group === group).length} 個片段</small></div><button type="button" aria-label={`${group} 群組選單`}>•••</button></header>
          {segments.filter((segment) => segment.group === group).map((segment, index) => <button
            type="button"
            key={segment.id}
            className={`ux-segment-row${selected.id === segment.id ? " selected" : ""}${segment.included ? "" : " excluded"}`}
            onClick={() => onSelect(segment.id)}
          >
            <span className="ux-drag" aria-hidden="true">⋮⋮</span>
            <span className="ux-thumb"><i>{String(index + 1).padStart(2, "0")}</i></span>
            <span className="ux-segment-copy"><b>{segment.title}</b><small>{segment.source}</small><span>{segment.start.toFixed(1)}–{segment.end.toFixed(1)} 秒 · AI {segment.score.toFixed(2)}</span></span>
            <span className="ux-row-tags"><i>{segment.sceneRole}</i><i className={segment.included ? "success" : "muted"}>{segment.included ? "已納入" : "已排除"}</i>{segment.locked && <i>已鎖定</i>}</span>
          </button>)}
        </section>)}
      </div>
    </div>
    <SegmentInspector selected={selected} groups={groups} onPatch={onPatch} />
  </section>;
}

function SegmentInspector({ selected, groups, onPatch }: { selected: Segment; groups: string[]; onPatch: (id: string, patch: Partial<Segment>) => void }) {
  const duration = Math.max(0, (selected.end - selected.start) / Math.max(0.25, selected.speed));
  return <aside className="ux-card ux-inspector">
    <CardHeader eyebrow="SEGMENT INSPECTOR" title="片段設定" />
    <div className="ux-inspector-preview"><span>{selected.title}</span><small>{selected.source}</small></div>
    <div className="ux-inspector-status"><label><input type="checkbox" checked={selected.included} onChange={(event) => onPatch(selected.id, { included: event.target.checked })} />納入成片</label><label><input type="checkbox" checked={selected.locked} onChange={(event) => onPatch(selected.id, { locked: event.target.checked })} />鎖定片段</label></div>
    <div className="ux-form-grid">
      <label className="wide">片段標題<input aria-label="片段標題" value={selected.title} onChange={(event) => onPatch(selected.id, { title: event.target.value })} /></label>
      <label className="wide">分組<select aria-label="片段分組" value={selected.group} onChange={(event) => onPatch(selected.id, { group: event.target.value })}>{groups.map((group) => <option key={group}>{group}</option>)}</select></label>
      <label>起點（秒）<input aria-label="片段起點" type="number" step="0.1" value={selected.start} onChange={(event) => onPatch(selected.id, { start: Number(event.target.value) })} /></label>
      <label>終點（秒）<input aria-label="片段終點" type="number" step="0.1" value={selected.end} onChange={(event) => onPatch(selected.id, { end: Number(event.target.value) })} /></label>
      <label>速度<select aria-label="片段速度" value={selected.speed} onChange={(event) => onPatch(selected.id, { speed: Number(event.target.value) })}><option value="0.75">0.75×</option><option value="1">1.00×</option><option value="1.15">1.15×</option><option value="1.25">1.25×</option><option value="1.5">1.50×</option></select></label>
      <label>成片長度<input aria-label="成片長度" readOnly value={`${duration.toFixed(1)} 秒`} /></label>
      <label className="wide">原音角色<select aria-label="原音角色" value={selected.audioRole} onChange={(event) => onPatch(selected.id, { audioRole: event.target.value as AudioRole })}><option value="keep">保留原音</option><option value="lower">降低原音</option><option value="mute">靜音</option><option value="bgm_only">只留 BGM</option></select></label>
      <label className="wide">分鏡備註<textarea aria-label="分鏡備註" value={selected.notes} onChange={(event) => onPatch(selected.id, { notes: event.target.value })} /></label>
    </div>
    <div className="ux-inspector-actions"><button type="button">預覽前段銜接</button><button type="button">預覽後段銜接</button><button type="button" className="primary">產生 5 秒預覽</button></div>
  </aside>;
}

function TuningWorkspace({ segments, selected, previewMode, tuningTab, onSelect, onPreviewMode, onTuningTab, onPatch, onNotify }: {
  segments: Segment[];
  selected: Segment;
  previewMode: PreviewMode;
  tuningTab: TuningTab;
  onSelect: (id: string) => void;
  onPreviewMode: (mode: PreviewMode) => void;
  onTuningTab: (tab: TuningTab) => void;
  onPatch: (id: string, patch: Partial<Segment>) => void;
  onNotify: (message: string) => void;
}) {
  return <section className="ux-tuning-grid">
    <aside className="ux-card ux-tuning-list">
      <CardHeader eyebrow="SEGMENTS" title="片段" />
      {segments.map((segment) => <button type="button" key={segment.id} className={selected.id === segment.id ? "selected" : ""} onClick={() => onSelect(segment.id)}><span className="ux-mini-thumb" /><span><b>{segment.title}</b><small>{segment.colorEnabled ? "調色啟用" : "調色停用"} · {roleLabel(segment.audioRole)}</small></span></button>)}
    </aside>
    <div className="ux-card ux-preview-stage">
      <CardHeader eyebrow="SHORT PREVIEW" title={selected.title} action={<button type="button" onClick={() => onNotify("已要求產生新的短預覽；原型不會呼叫 FFmpeg。")}>重新產生</button>} />
      <div className={`ux-video-frame mode-${previewMode}`}><span>{previewMode === "before" ? "BEFORE" : previewMode === "after" ? "AFTER" : "SEGMENT PREVIEW"}</span><strong>{selected.title}</strong><small>{previewMode === "before" ? "原始 D-Log 畫面" : previewMode === "after" ? "套用 Reference 與片段設定" : "包含調色、原音與 BGM"}</small></div>
      <div className="ux-preview-tabs" role="tablist" aria-label="預覽模式">{(["before", "after", "segment"] as PreviewMode[]).map((mode) => <button type="button" role="tab" aria-selected={previewMode === mode} className={previewMode === mode ? "active" : ""} key={mode} onClick={() => onPreviewMode(mode)}>{mode === "before" ? "Before" : mode === "after" ? "After" : "片段預覽"}</button>)}</div>
      <div className="ux-player-bar"><button type="button">▶</button><span><i /></span><time>00:03 / 00:08</time></div>
    </div>
    <aside className="ux-card ux-tuning-controls">
      <div className="ux-control-tabs" role="tablist" aria-label="調整類型"><button type="button" role="tab" aria-selected={tuningTab === "color"} className={tuningTab === "color" ? "active" : ""} onClick={() => onTuningTab("color")}>調色</button><button type="button" role="tab" aria-selected={tuningTab === "audio"} className={tuningTab === "audio" ? "active" : ""} onClick={() => onTuningTab("audio")}>音訊</button></div>
      {tuningTab === "color" ? <div className="ux-control-stack">
        <section><h3>Color Consistency</h3><label className="ux-switch"><input type="checkbox" checked={selected.colorEnabled} onChange={(event) => onPatch(selected.id, { colorEnabled: event.target.checked })} />啟用此片段調色</label></section>
        <section><label>技術 LUT<select defaultValue="dlog"><option value="dlog">DJI D-Log M to Rec.709</option><option>不套用</option></select></label><label>曝光<input type="range" min="-2" max="2" step="0.1" defaultValue="0.2" /></label><label>色溫<input type="range" min="-100" max="100" defaultValue="8" /></label><label>對比<input type="range" min="0.5" max="1.5" step="0.05" defaultValue="1.05" /></label><label>飽和度<input type="range" min="0" max="2" step="0.05" defaultValue="1.08" /></label></section>
        <button type="button" onClick={() => onNotify("已套用原型建議值。")}>套用系統建議</button>
      </div> : <div className="ux-control-stack">
        <section><h3>Audio Mixing</h3><label>原音角色<select value={selected.audioRole} onChange={(event) => onPatch(selected.id, { audioRole: event.target.value as AudioRole })}><option value="keep">保留原音</option><option value="lower">降低原音</option><option value="mute">靜音</option><option value="bgm_only">只留 BGM</option></select></label></section>
        <section><label>BGM 音量<input type="range" min="-40" max="0" defaultValue="-18" /></label><label>原音音量<input type="range" min="-40" max="6" defaultValue={selected.audioRole === "lower" ? -8 : 0} /></label><label>淡入<input type="number" defaultValue="0.2" step="0.1" /></label><label>淡出<input type="number" defaultValue="0.3" step="0.1" /></label><label className="ux-switch"><input type="checkbox" defaultChecked />LUFS 正規化</label></section>
        <button type="button" onClick={() => onNotify("已更新原型音訊預覽設定。")}>更新音訊預覽</button>
      </div>}
    </aside>
  </section>;
}

function OutputWorkspace({ segments, includedCount, estimatedDuration, confirmStoryboard, confirmRights, approved, onConfirmStoryboard, onConfirmRights, onApprove, onRender }: {
  segments: Segment[];
  includedCount: number;
  estimatedDuration: number;
  confirmStoryboard: boolean;
  confirmRights: boolean;
  approved: boolean;
  onConfirmStoryboard: (value: boolean) => void;
  onConfirmRights: (value: boolean) => void;
  onApprove: () => void;
  onRender: () => void;
}) {
  return <section className="ux-output-grid">
    <div className="ux-card ux-card-span-2">
      <CardHeader eyebrow="APPROVAL GATE" title="輸出前檢查" />
      <div className="ux-gate-list">
        <StatusRow label="素材與感知" value="已完成" state="success" />
        <StatusRow label="分鏡與剪點" value={`${includedCount}/${segments.length} 段納入`} state="success" />
        <StatusRow label="調色與音訊" value="設定可用" state="success" />
        <StatusRow label="人工核准" value={approved ? "已通過" : "待確認"} state={approved ? "success" : "warning"} />
      </div>
    </div>
    <aside className="ux-card ux-render-summary">
      <CardHeader eyebrow="FORMAL RENDER" title="正式輸出" />
      <dl><div><dt>預估長度</dt><dd>{formatDuration(estimatedDuration)}</dd></div><div><dt>使用片段</dt><dd>{includedCount} 段</dd></div><div><dt>輸出格式</dt><dd>MP4 · H.264</dd></div><div><dt>狀態</dt><dd>{approved ? "可建立工作" : "Gate 鎖定"}</dd></div></dl>
      <button type="button" className="primary large" disabled={!approved} onClick={onRender}>建立正式輸出工作</button>
    </aside>
    <div className="ux-card ux-approval-form">
      <CardHeader eyebrow="HUMAN REVIEW" title="人工確認" />
      <label><input type="checkbox" checked={confirmStoryboard} onChange={(event) => onConfirmStoryboard(event.target.checked)} /><span><b>我已確認分鏡、剪點與片段順序</b><small>確認成片敘事與預估長度符合需求。</small></span></label>
      <label><input type="checkbox" checked={confirmRights} onChange={(event) => onConfirmRights(event.target.checked)} /><span><b>我已確認 BGM 授權與輸出設定</b><small>確認授權資訊、署名與音量設定。</small></span></label>
      <button type="button" className="primary" disabled={approved} onClick={onApprove}>{approved ? "已核准" : "核准目前版本"}</button>
    </div>
  </section>;
}

function Metric({ label, value, meta, tone = "neutral" }: { label: string; value: string; meta: string; tone?: "neutral" | "warning" | "success" }) {
  return <article className={`ux-metric ${tone}`}><span>{label}</span><strong>{value}</strong><small>{meta}</small></article>;
}

function CardHeader({ eyebrow, title, action }: { eyebrow: string; title: string; action?: React.ReactNode }) {
  return <header className="ux-card-header"><div><span>{eyebrow}</span><h2>{title}</h2></div>{action}</header>;
}

function StatusRow({ label, value, state }: { label: string; value: string; state: "success" | "warning" }) {
  return <div className="ux-status-row"><span><i className={state} />{label}</span><b>{value}</b></div>;
}
