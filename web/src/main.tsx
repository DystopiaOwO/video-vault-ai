import { StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type { ChangeEvent, ReactNode } from "react";
import { api, AudioSegmentOverride, AudioSegmentSettings, AudioState, BgmTrack, ColorAdjustment, ColorSegmentPatch, ColorSegmentState, ColorState, ColorStatePatch, Job, Project, ProjectDetail, Segment, StoryboardState } from "./api";
import { RenderJobPanel } from "./components/render/RenderJobPanel";
import { ProjectDataLoadOptions, ProjectDataLoader } from "./projectDataLoader";
import "./styles.css";

type Workspace = "dashboard" | "assets" | "storyboard" | "color" | "audio" | "output";

const WORKSPACE_ITEMS: Array<{ id: Workspace; label: string; icon: string }> = [
  { id: "dashboard", label: "儀表板", icon: "▦" },
  { id: "assets", label: "素材與感知", icon: "▣" },
  { id: "storyboard", label: "分鏡審核", icon: "▤" },
  { id: "color", label: "調色", icon: "◌" },
  { id: "audio", label: "音訊", icon: "♫" },
  { id: "output", label: "輸出", icon: "↥" },
];

export function App() {
  if (window.location.pathname === "/bgm") return <BgmPage />;
  const [projects, setProjects] = useState<Project[]>([]);
  const [currentId, setCurrentId] = useState<number>(0);
  const [workspace, setWorkspace] = useState<Workspace>("dashboard");
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [bgmTracks, setBgmTracks] = useState<BgmTrack[]>([]);
  const [notes, setNotes] = useState("");
  const [message, setMessage] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const currentIdRef = useRef(0);
  const mountedRef = useRef(true);
  const loaderRef = useRef<ProjectDataLoader | null>(null);
  if (!loaderRef.current) {
    loaderRef.current = new ProjectDataLoader(
      { project: api.project, jobs: api.jobs },
      (projectId) => projectId === currentIdRef.current,
      () => mountedRef.current,
      (project, nextJobs) => {
        setDetail(project);
        setJobs(nextJobs);
      },
      (error) => setMessage(`狀態更新失敗：${error instanceof Error ? error.message : "未知錯誤"}`),
    );
  }

  useEffect(() => {
    loadProjects();
    api.bgm().then(setBgmTracks).catch((error) => setMessage(`BGM 載入失敗：${error instanceof Error ? error.message : "未知錯誤"}`));
  }, []);

  function loadProjects() {
    return api.projects().then((rows) => {
      setProjects(rows);
      setCurrentId((id) => id || rows[0]?.id || 0);
    }).catch((error) => setMessage(`專案載入失敗：${error instanceof Error ? error.message : "未知錯誤"}`));
  }

  useEffect(() => {
    currentIdRef.current = currentId;
  }, [currentId]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      loaderRef.current?.invalidate();
    };
  }, []);

  function loadProjectData(requestedProjectId: number, options: ProjectDataLoadOptions = {}): Promise<Job[]> {
    return loaderRef.current?.load(requestedProjectId, options) || Promise.resolve([]);
  }

  useEffect(() => {
    if (!currentId) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      if (cancelled) return;
      await loadProjectData(currentId);
      if (!cancelled) timer = window.setTimeout(() => void poll(), 1500);
    };
    void poll();
    return () => {
      cancelled = true;
      loaderRef.current?.invalidate();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [currentId]);

  async function review(action: "approve" | "reject") {
    if (!detail) return;
    setMessage("送出中...");
    action === "approve" ? await api.approve(detail.project.id, notes) : await api.reject(detail.project.id, notes);
    setNotes("");
    setMessage(action === "approve" ? "已核准專案" : "已退回修改");
    setDetail(await api.project(detail.project.id));
  }

  async function revise() {
    if (!detail) return;
    setMessage("正在依備註重建故事...");
    await api.revise(detail.project.id, notes);
    setMessage("故事整理已依備註重建");
    setDetail(await api.project(detail.project.id));
  }

  async function createProject() {
    setMessage("正在建立專案...");
    const result = await api.createProject(newProjectName);
    setNewProjectName("");
    await loadProjects();
    setCurrentId(result.id);
    setMessage("專案已建立，下一步請匯入素材。");
  }

  async function refreshProject(projectId: number, options: ProjectDataLoadOptions = {}): Promise<Job[]> {
    return loadProjectData(projectId, options);
  }

  return (
    <main className="app-shell">
      <aside className="app-sidebar">
        <div className="brand-lockup"><span className="brand-mark">V</span><div><strong>Video Vault AI</strong><small>本機影片工作台</small></div></div>
        <nav className="sidebar-nav" aria-label="主要導覽">
          {WORKSPACE_ITEMS.map((item) => <button key={item.id} className={`sidebar-nav-item${workspace === item.id ? " active" : ""}`} onClick={() => setWorkspace(item.id)}><span className="sidebar-icon" aria-hidden="true">{item.icon}</span>{item.label}</button>)}
          <a className="sidebar-nav-item" href="/bgm"><span className="sidebar-icon" aria-hidden="true">♫</span>BGM 資料庫</a>
        </nav>
        <div className="sidebar-projects">
          <div className="sidebar-section-heading"><span>專案</span><button className="icon-button" title="新增專案" onClick={() => document.getElementById("new-project-name")?.focus()}>＋</button></div>
          <div className="new-project">
            <input id="new-project-name" value={newProjectName} onChange={(e) => setNewProjectName(e.target.value)} placeholder="新專案名稱" />
            <button className="primary-button" onClick={createProject}>新增專案</button>
          </div>
          <div className="project-list">
            {projects.map((p) => <button key={p.id} className={p.id === currentId ? "project active" : "project"} onClick={() => setCurrentId(p.id)}><span className="project-dot" /><span className="project-copy"><b>{p.name}</b><small>{p.status === "approved" ? "可輸出" : "待審核"} · {p.video_count ?? 0} 支素材</small></span><span className="project-more">···</span></button>)}
            {!projects.length && <p className="sidebar-empty">尚未建立專案</p>}
          </div>
        </div>
        <div className="sidebar-storage"><div className="storage-heading"><span>儲存空間</span><strong>47%</strong></div><div className="storage-bar"><span style={{ width: "47%" }} /></div><small>1.42 TB / 3 TB</small><button>⚡ 升級方案</button></div>
      </aside>
      <div className="app-main">
        <header className="topbar">
          <label className="project-switcher"><span className="topbar-label">目前專案</span><select aria-label="目前專案" value={currentId || ""} onChange={(event) => setCurrentId(Number(event.target.value))}><option value="">選擇專案</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select><span className="select-caret">⌄</span></label>
          <label className="global-search"><span aria-hidden="true">⌕</span><input aria-label="全域搜尋" placeholder="搜尋素材、專案、任務..." /><kbd>⌘ K</kbd></label>
          <div className="topbar-actions"><button className="topbar-icon" title="通知">♧<sup>{jobs.some((job) => ["queued", "running", "cancelling"].includes(job.status)) ? "!" : ""}</sup></button><button className="topbar-icon" title="說明">?</button><div className="user-menu"><span className="avatar">A</span><span><b>本機使用者</b><small>影片管理員</small></span><span>⌄</span></div></div>
        </header>
        <section className="app-content">
          {message && <div className="notice">{message}</div>}
          {!detail ? <div className="card empty-project"><span className="empty-icon">＋</span><h2>尚未選擇專案</h2><p>先建立一個專案，再匯入你的影片素材。</p></div> : <ProjectView key={detail.project.id} detail={detail} jobs={jobs} bgmTracks={bgmTracks} workspace={workspace} setWorkspace={setWorkspace} notes={notes} setNotes={setNotes} setMessage={setMessage} refreshProject={refreshProject} review={review} revise={revise} />}
        </section>
      </div>
    </main>
  );
}

function BgmPage() {
  const [tracks, setTracks] = useState<BgmTrack[]>([]);
  useEffect(() => {
    api.bgm().then(setTracks);
  }, []);
  return (
    <main className="app-shell">
      <aside className="app-sidebar">
        <div className="brand-lockup"><span className="brand-mark">V</span><div><strong>Video Vault AI</strong><small>本機影片工作台</small></div></div>
        <nav className="sidebar-nav" aria-label="主要導覽">
          <a className="sidebar-nav-item" href="/"><span className="sidebar-icon">▦</span>儀表板</a>
          <a className="sidebar-nav-item active" href="/bgm"><span className="sidebar-icon">♫</span>BGM 資料庫</a>
          <a className="sidebar-nav-item" href="/classic-bgm"><span className="sidebar-icon">＋</span>匯入 BGM</a>
        </nav>
        <div className="sidebar-storage"><div className="storage-heading"><span>本地音樂庫</span><strong>{tracks.length} 首</strong></div><small>授權資訊與來源會隨專案交接包保留。</small></div>
      </aside>
      <div className="app-main">
        <header className="topbar"><div className="topbar-page-label"><b>BGM 資料庫</b><small>全域音樂總覽</small></div><label className="global-search"><span aria-hidden="true">⌕</span><input aria-label="搜尋 BGM" placeholder="搜尋音樂、作者、情緒..." /></label><div className="topbar-actions"><a className="primary-button topbar-link" href="/classic-bgm">＋ 匯入 BGM</a></div></header>
        <section className="app-content"><div className="project-page"><div className="page-heading"><div><p className="eyebrow">Library · Attribution ready</p><h2>本地 BGM 總覽</h2><p className="page-subtitle">共 {tracks.length} 首音樂；專案頁只會顯示該專案使用的曲目。</p></div></div><div className="bgm-library-grid">{tracks.map((track) => <article className="bgm-card" key={track.id}><div className="bgm-cover">♫</div><div className="bgm-card-content"><div className="bgm-title-row"><div><h3>{track.title}</h3><p>{track.artist || "未知作者"}</p></div><span className="pill">{track.mood || "未分類"}</span></div><div className="bgm-meta"><span>{track.license_name || "未填授權"}</span><span>{track.duration_seconds ? `${track.duration_seconds}s` : "長度未知"}</span></div><p className="bgm-attribution">{track.attribution_text || "尚未填寫 YouTube 署名文字。"}</p>{track.source_url && <a href={track.source_url} target="_blank" rel="noreferrer">查看來源 →</a>}</div></article>)}{!tracks.length && <div className="empty-state large-empty"><span className="empty-icon">♫</span><h3>尚無 BGM</h3><p>先匯入一首有授權資訊的音樂，再到專案中選用。</p></div>}</div></div></section>
      </div>
    </main>
  );
}

function ProjectView({ detail, jobs, bgmTracks, workspace, setWorkspace, notes, setNotes, setMessage, refreshProject, review, revise }: {
  detail: ProjectDetail;
  jobs: Job[];
  bgmTracks: BgmTrack[];
  workspace: Workspace;
  setWorkspace: (workspace: Workspace) => void;
  notes: string;
  setNotes: (value: string) => void;
  setMessage: (value: string) => void;
  refreshProject: (projectId: number, options?: ProjectDataLoadOptions) => Promise<Job[]>;
  review: (action: "approve" | "reject") => void;
  revise: () => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const refreshCurrentProject = (options: ProjectDataLoadOptions = {}) => refreshProject(detail.project.id, options);
  return (
    <div className="project-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">WebUI-first · Human-in-the-loop review</p>
          <h2>{detail.project.name}</h2>
          <p className="page-subtitle">專案工作台 <span>·</span> {detail.folder || "本機專案資料夾"}</p>
        </div>
        <div className="heading-actions"><Status value={detail.project.status} /><button onClick={() => setWorkspace("storyboard")}>進入分鏡審核 <span aria-hidden="true">→</span></button><button className="primary-button" onClick={() => setWorkspace("output")}>輸出檢查</button></div>
      </div>
      <Workflow detail={detail} active={workspace} onNavigate={setWorkspace} />
      {workspace === "dashboard" && <DashboardWorkspace detail={detail} jobs={jobs} notes={notes} setNotes={setNotes} review={review} revise={revise} setMessage={setMessage} refreshProject={refreshCurrentProject} onNavigate={setWorkspace} onUpload={uploadFiles} onAnalyze={() => void analyze(true)} onBuildPlan={() => void buildPlan()} onExport={exportProject} onFormalRender={() => void startFormalRender()} submitting={submitting} />}
      {workspace === "assets" && <AssetsWorkspace detail={detail} onUpload={uploadFiles} onAnalyze={() => void analyze(true)} onAnalyzeOne={(videoId) => void analyzeOne(videoId)} onBuildPlan={() => void buildPlan()} setMessage={setMessage} refreshProject={refreshCurrentProject} />}
      {workspace === "storyboard" && <div className="workspace-columns"><div className="workspace-main"><StoryboardPanel detail={detail} setMessage={setMessage} refreshProject={refreshCurrentProject} /><SegmentTable detail={detail} /></div><div className="workspace-rail"><ReviewCard detail={detail} notes={notes} setNotes={setNotes} review={review} revise={revise} /></div></div>}
      {workspace === "color" && <div className="workspace-main wide-workspace"><ColorConsistencyPanel detail={detail} setMessage={setMessage} refreshProject={refreshCurrentProject} /></div>}
      {workspace === "audio" && <div className="workspace-main wide-workspace"><AudioMixingPanel detail={detail} bgmTracks={bgmTracks} setMessage={setMessage} refreshProject={refreshCurrentProject} /></div>}
      {workspace === "output" && <div className="workspace-columns"><div className="workspace-main"><RenderJobPanel jobs={jobs} projectId={detail.project.id} setMessage={setMessage} refreshProject={refreshCurrentProject} /><OutputCard detail={detail} jobs={jobs} submitting={submitting} onExport={exportProject} onFormalRender={() => void startFormalRender()} /></div><div className="workspace-rail"><ReviewCard detail={detail} notes={notes} setNotes={setNotes} review={review} revise={revise} /><WorkflowSkeleton detail={detail} /></div></div>}
    </div>
  );

  async function exportProject(kind: "hyperframes" | "hyperframes-render" | "opencut" | "opencut-render") {
    try {
      setMessage("工作已送出，請看 Render Job 狀態與進度。");
      const result = kind.startsWith("hyperframes")
        ? await api.hyperframesJob(detail.project.id, kind === "hyperframes-render")
        : await api.opencutJob(detail.project.id, kind === "opencut-render");
      await refreshCurrentProject({ forceFresh: true });
      if (!result.ok) {
        setMessage(`工作啟動失敗：${result.error || result.message || "工作未成功送出"}`);
        return;
      }
      setMessage(result.message || "工作已開始");
    } catch (error) {
      setMessage(`工作啟動失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  async function startFormalRender() {
    if (!detail.can_render) {
      setMessage(`正式輸出被擋下：${detail.render_gate_reason}`);
      return;
    }
    const requestedProjectId = detail.project.id;
    try {
      setSubmitting(true);
      setMessage("正在建立正式輸出…");
      const result = await api.createRenderJob(requestedProjectId);
      if (!result.ok) {
        setMessage(`正式輸出失敗：${result.error || "建立 Render Job 未成功"}`);
        return;
      }
      await refreshProject(requestedProjectId, { forceFresh: true });
      setMessage(result.created ? "正式輸出已排入佇列" : "正式輸出工作已在執行中");
    } catch (error) {
      setMessage(`正式輸出啟動失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally {
      setSubmitting(false);
    }
  }

  async function uploadFiles(event: ChangeEvent<HTMLInputElement>) {
    const files = event.target.files;
    if (!files?.length) return;
    setMessage("正在匯入素材...");
    const result = await api.uploadProject(detail.project.id, files);
    event.target.value = "";
    setMessage(result.ok ? `已匯入 ${result.files?.length || 0} 支素材，下一步請跑內容感知。` : result.error || "匯入失敗");
    await refreshCurrentProject();
  }

  async function analyze(force: boolean) {
    setMessage(force ? "已送出全部重跑感知。" : "已送出待感知素材。");
    const result = await api.analyzeJob(detail.project.id, force);
    setMessage(result.message || "內容感知工作已開始");
    await refreshCurrentProject();
  }

  async function analyzeOne(videoId: number) {
    setMessage("已送出單支素材感知，請看工作狀態百分比。");
    const result = await api.analyzeVideo(detail.project.id, videoId);
    setMessage(result.message || "單支素材感知已開始");
    await refreshCurrentProject();
  }

  async function buildPlan() {
    setMessage("正在產生故事整理...");
    await api.buildPlan(detail.project.id);
    setMessage("故事整理已更新，請審核片段。");
    await refreshCurrentProject();
  }

}

type ExportKind = "hyperframes" | "hyperframes-render" | "opencut" | "opencut-render";

function DashboardWorkspace({ detail, jobs, notes, setNotes, review, revise, setMessage, refreshProject, onNavigate, onUpload, onAnalyze, onBuildPlan, onExport, onFormalRender, submitting }: {
  detail: ProjectDetail;
  jobs: Job[];
  notes: string;
  setNotes: (value: string) => void;
  review: (action: "approve" | "reject") => void;
  revise: () => void;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]>;
  onNavigate: (workspace: Workspace) => void;
  onUpload: (event: ChangeEvent<HTMLInputElement>) => void;
  onAnalyze: () => void;
  onBuildPlan: () => void;
  onExport: (kind: ExportKind) => Promise<void>;
  onFormalRender: () => void;
  submitting: boolean;
}) {
  const totalSegments = detail.segments.length;
  const includedSegments = detail.storyboard?.summary?.included_segments ?? detail.segments.filter((segment) => segment.include).length;
  const estimatedSeconds = detail.storyboard?.summary?.estimated_duration_seconds ?? detail.segments.filter((segment) => segment.include).reduce((sum, segment) => sum + Math.max(0, (segment.end_seconds - segment.start_seconds) / Math.max(.01, segment.speed || 1)), 0);
  const pendingReview = detail.segments.filter((segment) => !segment.user_notes).length;
  const activeJobs = jobs.filter((job) => ["queued", "running", "cancelling"].includes(job.status));
  return <>
    <div className="metric-strip">
      <Metric icon="▤" label="素材數量" value={String(detail.clips.length)} detail="支影片" />
      <Metric icon="✂" label="已選片段" value={String(includedSegments)} detail={`/ ${totalSegments || 0} 段`} />
      <Metric icon="◷" label="預估成片長度" value={time(estimatedSeconds)} detail="依目前分鏡順序" />
      <Metric icon="!" tone="warning" label="待處理項目" value={String(pendingReview)} detail="個片段" />
      <Metric icon="↥" tone={detail.can_render ? "success" : "neutral"} label="輸出狀態" value={detail.can_render ? "可輸出" : "待核准"} detail={detail.can_render ? "Approval gate 已通過" : "需要人工審核"} />
    </div>
    <div className="dashboard-grid dashboard-top-grid">
      <Card title="最近素材"><div className="clip-list compact-clip-list">{detail.clips.slice(0, 5).map((clip) => <div className="clip-list-row" key={clip.clip_id}><span className="clip-placeholder">▣</span><span><b>{clip.filename}</b><small>{clip.status} · {clip.segment_count} 段 · {Math.round(clip.duration_seconds || 0)} 秒</small></span><Status value={clip.status} /></div>)}{!detail.clips.length && <p className="empty-state">尚未匯入影片素材。</p>}</div><button className="link-button" onClick={() => onNavigate("assets")}>查看全部素材 <span>→</span></button></Card>
      <Card title="專案健康度"><div className="health-score"><strong>{detail.can_render ? "100" : detail.segments.length ? "82" : "0"}</strong><span>/100</span></div><div className="health-bars"><HealthBar label="素材完整性" value={detail.clips.length ? 92 : 0} /><HealthBar label="故事整理" value={detail.segments.length ? 82 : 0} /><HealthBar label="分鏡覆蓋率" value={totalSegments ? Math.round((includedSegments / totalSegments) * 100) : 0} /></div><p className="health-message">{detail.can_render ? "狀態良好，可以進入正式輸出。" : detail.render_gate_reason || "完成分鏡與人工審核後即可輸出。"}</p></Card>
      <Card title="快速操作"><div className="quick-action-grid"><button onClick={() => onNavigate("assets")}><span>▣</span><b>匯入素材</b><small>新增影片或重新感知</small></button><button onClick={() => onNavigate("storyboard")}><span>▤</span><b>分鏡審核</b><small>檢視與調整片段順序</small></button><button onClick={() => onNavigate("color")}><span>◌</span><b>調色工作區</b><small>參考畫面與套用建議</small></button><button onClick={() => onNavigate("audio")}><span>♫</span><b>音訊混音器</b><small>調整 BGM 與原音</small></button></div></Card>
    </div>
    <RenderJobPanel jobs={jobs} projectId={detail.project.id} setMessage={setMessage} refreshProject={refreshProject} />
    {activeJobs.length > 0 && <div className="active-job-banner"><span className="live-dot" />目前有 {activeJobs.length} 個背景工作執行中，進度會自動更新。</div>}
    <div className="dashboard-grid dashboard-bottom-grid"><ReviewCard detail={detail} notes={notes} setNotes={setNotes} review={review} revise={revise} /><OutputCard detail={detail} jobs={jobs} submitting={submitting} onExport={onExport} onFormalRender={onFormalRender} /></div>
    <WorkflowSkeleton detail={detail} />
    <div className="dashboard-intake"><div><b>下一步</b><p>匯入素材後先跑內容感知，再進行故事整理與人工審核。</p></div><div className="row"><label className="file-button"><span>＋ 匯入影片</span><input type="file" multiple accept="video/*" onChange={onUpload} /></label><button onClick={onAnalyze}>全部重跑感知</button><button onClick={onBuildPlan}>產生故事整理</button></div></div>
  </>;
}

function AssetsWorkspace({ detail, onUpload, onAnalyze, onAnalyzeOne, onBuildPlan, setMessage, refreshProject }: {
  detail: ProjectDetail;
  onUpload: (event: ChangeEvent<HTMLInputElement>) => void;
  onAnalyze: () => void;
  onAnalyzeOne: (videoId: number) => void;
  onBuildPlan: () => void;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]>;
}) {
  return <div className="workspace-main wide-workspace"><div className="section-toolbar"><div><p className="eyebrow">Step 1 · Step 2</p><h3>匯入素材與內容感知</h3><p className="muted">每個專案擁有自己的素材資料夾；內容感知完成後可直接編輯描述。</p></div><div className="row"><label className="file-button primary-button"><span>＋ 匯入影片</span><input type="file" multiple accept="video/*" onChange={onUpload} /></label><button onClick={onAnalyze}>全部重跑感知</button><button className="primary-button" onClick={onBuildPlan}>產生故事整理</button></div></div><div className="asset-grid">{detail.clips.map((clip) => <article className="asset-card" key={clip.clip_id}><div className="asset-thumb"><span>▣</span><small>{Math.round(clip.duration_seconds || 0)}s</small></div><div className="asset-card-body"><div className="asset-title-line"><div><h3>{clip.filename}</h3><p>{clip.clip_id} · {clip.time_of_day || "時間未判斷"}</p></div><Status value={clip.status} /></div><div className="asset-stats"><span>{clip.segment_count} 個片段</span><span>{clip.detected_category || "待分類"}</span></div><ClipSummary projectId={detail.project.id} clip={clip} setMessage={setMessage} refreshProject={refreshProject} /><button className="small-button" onClick={() => onAnalyzeOne(clip.video_id)}>↻ 單獨重跑感知</button></div></article>)}{!detail.clips.length && <div className="empty-state large-empty"><span className="empty-icon">＋</span><h3>把影片放進這個專案</h3><p>支援多支影片；匯入後會依專案資料夾分開管理。</p></div>}</div></div>;
}

function ReviewCard({ detail, notes, setNotes, review, revise }: { detail: ProjectDetail; notes: string; setNotes: (value: string) => void; review: (action: "approve" | "reject") => void; revise: () => void }) {
  return <Card title="人工審核"><div className={`approval-callout ${detail.can_render ? "approved" : "pending"}`}><span>{detail.can_render ? "✓" : "!"}</span><div><b>{detail.can_render ? "專案已核准" : "等待人工核准"}</b><small>{detail.can_render ? "目前輸入與核准版本一致，可正式輸出。" : detail.render_gate_reason || "完成審核後才能正式輸出。"}</small></div></div><textarea value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="輸入審核備註或修改方向..." /><div className="row"><button className="primary-button" onClick={() => review("approve")}>✓ 核准專案</button><button className="danger" onClick={() => review("reject")}>退回修改</button><button onClick={revise}>依備註重建故事</button></div></Card>;
}

function OutputCard({ detail, jobs, submitting, onExport, onFormalRender }: { detail: ProjectDetail; jobs: Job[]; submitting: boolean; onExport: (kind: ExportKind) => Promise<void>; onFormalRender: () => void }) {
  const active = jobs.some((job) => Boolean(job.job_id) && ["queued", "running", "cancelling"].includes(job.status));
  return <Card title="輸出與交接"><div className="output-status"><span className={detail.can_render ? "live-dot" : "status-dot"} />{detail.can_render ? "已通過 Approval gate" : "尚未核准正式輸出"}</div><div className="output-action-list"><button onClick={() => void onExport("hyperframes")}>建立初剪專案 <span>→</span></button><button disabled={!detail.can_render} onClick={() => void onExport("hyperframes-render")}>快速輸出 MP4 <span>→</span></button><button className="primary-button" disabled={submitting || !detail.can_render || active} onClick={onFormalRender}>{submitting ? "正在建立正式輸出…" : active ? "正式輸出執行中" : "正式輸出（Render Job）"}</button><button onClick={() => void onExport("opencut")}>OpenCut 素材包 <span>→</span></button><button disabled={!detail.can_render} onClick={() => void onExport("opencut-render")}>OpenCut 調色片段 <span>→</span></button></div></Card>;
}

function Metric({ icon, label, value, detail, tone = "default" }: { icon: string; label: string; value: string; detail: string; tone?: "default" | "success" | "warning" | "neutral" }) {
  return <div className={`metric metric-${tone}`}><span className="metric-icon">{icon}</span><div><small>{label}</small><strong>{value}</strong><span>{detail}</span></div></div>;
}

function HealthBar({ label, value }: { label: string; value: number }) {
  return <div className="health-bar"><div><span>{label}</span><b>{value}</b></div><div className="health-track"><span style={{ width: `${Math.max(0, Math.min(100, value))}%` }} /></div></div>;
}

export function normalizeStoryboardOrders(next: StoryboardState): StoryboardState {
  const result = structuredClone(next);
  result.groups = result.groups.slice().sort((a, b) => a.order - b.order).map((group, index) => ({ ...group, order: index + 1 }));
  result.groups.forEach((group) => {
    Object.entries(result.segments)
      .filter(([, item]) => item.group_id === group.group_id)
      .sort(([, a], [, b]) => a.order - b.order)
      .forEach(([segmentId], index) => { result.segments[segmentId].order = index + 1; });
  });
  return result;
}

export function reorderStoryboardSegments(next: StoryboardState, dragId: string, targetId: string, position: "before" | "after"): StoryboardState {
  if (!dragId || !targetId || dragId === targetId || !next.segments[dragId] || !next.segments[targetId]) return next;
  const result = structuredClone(next);
  const targetGroup = result.segments[targetId].group_id;
  const sourceGroup = result.segments[dragId].group_id;
  const ids = Object.entries(result.segments)
    .filter(([segmentId, item]) => item.group_id === targetGroup && segmentId !== dragId)
    .sort(([, a], [, b]) => a.order - b.order)
    .map(([segmentId]) => segmentId);
  const targetIndex = ids.indexOf(targetId);
  ids.splice(Math.max(0, targetIndex + (position === "after" ? 1 : 0)), 0, dragId);
  result.segments[dragId] = { ...result.segments[dragId], group_id: targetGroup, manual_group: Boolean(result.segments[dragId].manual_group || sourceGroup !== targetGroup), manual_order: true };
  ids.forEach((segmentId, index) => { result.segments[segmentId].order = index + 1; result.segments[segmentId].manual_order = true; });
  return normalizeStoryboardOrders(result);
}

export function effectiveSegmentColorEnabled(projectEnabled: boolean, override?: ColorSegmentState): boolean {
  if (override?.excluded) return false;
  if (override && "enabled" in override) return Boolean(override.enabled);
  return projectEnabled;
}

export function colorTogglePatch(state: ColorState, segmentId: string): ColorSegmentPatch {
  const current = state.segments[segmentId];
  const enabled = effectiveSegmentColorEnabled(state.enabled, current);
  return { enabled: !enabled, locked: Boolean(current?.locked), excluded: Boolean(current?.excluded), ...(current?.applied ? { applied: { ...current.applied } } : {}) };
}

export function colorResetPatch(): null { return null; }

export function StoryboardPanel({ detail, setMessage, refreshProject }: { detail: ProjectDetail; setMessage: (value: string) => void; refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]> }) {
  const emptyStoryboard: StoryboardState = { schema_version: 1, groups: [], segments: {} };
  const [state, setState] = useState<StoryboardState>(detail.storyboard || emptyStoryboard);
  const [lastServerState, setLastServerState] = useState<StoryboardState>(detail.storyboard || emptyStoryboard);
  const [isDirty, setIsDirty] = useState(false);
  const [saveInProgress, setSaveInProgress] = useState(false);
  const [busy, setBusy] = useState("");
  const [dragged, setDragged] = useState("");
  const [dropTarget, setDropTarget] = useState<{ segmentId: string; position: "before" | "after" } | null>(null);
  const [newGroup, setNewGroup] = useState("");
  const [rangeStart, setRangeStart] = useState(0);
  const [rangeDuration, setRangeDuration] = useState(8);
  const [selectedSegmentId, setSelectedSegmentId] = useState("");
  const [previewItems, setPreviewItems] = useState<Array<{ kind: string; url?: string; duration_seconds: number }>>([]);
  const [segmentDrafts, setSegmentDrafts] = useState<Record<string, { start_seconds: number; end_seconds: number; speed: number }>>({});
  const [loadedProjectId, setLoadedProjectId] = useState(detail.project.id);

  useEffect(() => {
    const incoming = detail.storyboard || emptyStoryboard;
    setLastServerState(incoming);
    if (loadedProjectId !== detail.project.id) {
      setLoadedProjectId(detail.project.id);
      setState(incoming);
      setIsDirty(false);
      setSaveInProgress(false);
      setSelectedSegmentId("");
      setPreviewItems([]);
      return;
    }
    if (!isDirty && !saveInProgress) setState(incoming);
  }, [detail.project.id, detail.storyboard, isDirty, saveInProgress]);

  const rowsByGroup = (groupId: string) => detail.segments
    .filter((row) => state.segments[row.segment_id]?.group_id === groupId)
    .sort((a, b) => (state.segments[a.segment_id]?.order || 0) - (state.segments[b.segment_id]?.order || 0));

  function normalizeOrders(next: StoryboardState): StoryboardState { return normalizeStoryboardOrders(next); }

  function rowsByGroupFromState(current: StoryboardState, groupId: string): string[] {
    return Object.entries(current.segments)
      .filter(([, item]) => item.group_id === groupId)
      .sort(([, a], [, b]) => a.order - b.order)
      .map(([segmentId]) => segmentId);
  }

  function updateLocal(next: StoryboardState) {
    setState(normalizeOrders(next));
    setIsDirty(true);
  }

  async function generate(force = false) {
    if (isDirty) { setMessage("請先儲存未儲存的分鏡變更，再重新產生。"); return; }
    setBusy("generate");
    const result = await api.generateStoryboard(detail.project.id, force);
    setBusy("");
    if (!result.ok || !result.storyboard) { setMessage(`分鏡建立失敗：${result.error || "未知錯誤"}`); return; }
    setState(result.storyboard);
    setLastServerState(result.storyboard);
    setIsDirty(false);
    setMessage(force ? "分鏡已重新產生，鎖定片段、人工排序、備註與自訂群組已保留。" : "分鏡已建立，請開始審核與排序。");
    await refreshProject();
  }

  async function save(next = state) {
    const normalized = normalizeOrders(next);
    setSaveInProgress(true);
    setBusy("save");
    const result = await api.updateStoryboard(detail.project.id, normalized);
    if (!result.ok) {
      setSaveInProgress(false);
      setBusy("");
      setMessage(`分鏡儲存失敗：${result.error || "未知錯誤"}`);
      return;
    }
    const saved = result.storyboard || normalized;
    setState(saved);
    setLastServerState(saved);
    setIsDirty(false);
    await refreshProject();
    setSaveInProgress(false);
    setBusy("");
    setMessage(
      result.approval_invalidated
        ? "分鏡已儲存，輸出內容有變更，請重新核准後再正式輸出。"
        : "分鏡已儲存，這次未修改輸出內容，既有核准仍有效。",
    );
  }

  function editSegment(segmentId: string, patch: Partial<StoryboardState["segments"][string]>) {
    const next = structuredClone(state);
    next.segments[segmentId] = { ...next.segments[segmentId], ...patch };
    updateLocal(next);
  }

  function reorder(dragId: string, targetId: string, position: "before" | "after") {
    if (!dragId || !targetId || dragId === targetId) return;
    const next = reorderStoryboardSegments(state, dragId, targetId, position);
    updateLocal(next);
    setDragged("");
    setDropTarget(null);
  }

  function dropOnGroup(groupId: string) {
    if (!dragged) return;
    const next = structuredClone(state);
    const ids = rowsByGroupFromState(next, groupId).filter((id) => id !== dragged);
    ids.push(dragged);
    ids.forEach((id, index) => { next.segments[id].manual_group = true; next.segments[id].manual_order = true; next.segments[id].group_id = groupId; next.segments[id].order = index + 1; });
    updateLocal(next);
    setDragged("");
    setDropTarget(null);
  }

  function moveVertical(segmentId: string, delta: number) {
    const groupId = state.segments[segmentId]?.group_id;
    if (!groupId) return;
    const ids = rowsByGroupFromState(state, groupId);
    const index = ids.indexOf(segmentId);
    const target = index + delta;
    if (index < 0 || target < 0 || target >= ids.length) return;
    reorder(segmentId, ids[target], delta < 0 ? "before" : "after");
  }

  function moveToGroup(segmentId: string, groupId: string) {
    const next = structuredClone(state);
    const ids = rowsByGroupFromState(next, groupId).filter((id) => id !== segmentId);
    ids.push(segmentId);
    ids.forEach((id, index) => { next.segments[id].manual_group = true; next.segments[id].manual_order = true; next.segments[id].group_id = groupId; next.segments[id].order = index + 1; });
    updateLocal(next);
  }

  function addGroup() {
    const title = newGroup.trim();
    if (!title) return;
    updateLocal({ ...state, groups: [...state.groups, { group_id: `custom_${Date.now()}`, title, category: "custom", order: state.groups.length + 1 }] });
    setNewGroup("");
  }

  function moveGroup(groupId: string, delta: number) {
    const index = state.groups.findIndex((group) => group.group_id === groupId);
    const target = index + delta;
    if (index < 0 || target < 0 || target >= state.groups.length) return;
    const groups = [...state.groups];
    [groups[index], groups[target]] = [groups[target], groups[index]];
    updateLocal({ ...state, groups });
  }

  function deleteEmptyGroup(groupId: string) {
    if (rowsByGroupFromState(state, groupId).length) return;
    updateLocal({ ...state, groups: state.groups.filter((group) => group.group_id !== groupId) });
  }

  function editGroup(groupId: string, title: string) {
    updateLocal({ ...state, groups: state.groups.map((group) => group.group_id === groupId ? { ...group, title } : group) });
  }

  async function thumbnail(segment: Segment, ratio: number, force = false) {
    editSegment(segment.segment_id, { thumbnail_time_ratio: ratio });
    const result = await api.storyboardThumbnail(detail.project.id, segment.segment_id, ratio, force);
    if (!result.ok) { setMessage(`代表畫格產生失敗：${result.error || "未知錯誤"}`); return; }
    setState((current) => ({ ...current, segments: { ...current.segments, [segment.segment_id]: { ...current.segments[segment.segment_id], thumbnail_time_ratio: ratio, thumbnail_url: result.url } } }));
    setMessage(result.cache_hit ? "代表畫格已從快取載入。" : "代表畫格已產生。請儲存分鏡以保留位置。");
  }

  async function preview(mode: "segment" | "transition" | "range", segmentId?: string, force = false) {
    setBusy("preview");
    const selected = segmentId || selectedSegmentId;
    const start = mode === "range" ? (selected ? timelineStart(selected) : rangeStart) : 0;
    const result = await api.storyboardPreview(detail.project.id, { mode, segmentId: selected, durationSeconds: mode === "segment" ? 5 : rangeDuration, timelineStartSeconds: start, storyboardState: isDirty ? state : undefined, force });
    setBusy("");
    if (!result.ok) { setMessage(`分鏡預覽失敗：${result.error || "未知錯誤"}`); return; }
    setPreviewItems(result.previews?.map((item) => ({ kind: item.kind, url: item.url, duration_seconds: item.duration_seconds })) || (result.url ? [{ kind: mode, url: result.url, duration_seconds: Number(result.duration_seconds || 0) }] : []));
    setMessage(force ? "分鏡預覽已強制重新產生。" : result.cache_hit ? "分鏡預覽已從快取載入。" : "分鏡預覽已產生。");
  }

  function timelineStart(segmentId: string): number {
    let total = 0;
    for (const group of state.groups) {
      for (const id of rowsByGroupFromState(state, group.group_id)) {
        if (!state.segments[id].included) continue;
        if (id === segmentId) return total;
        const row = detail.segments.find((item) => item.segment_id === id);
        if (row) total += Math.max(0, (row.end_seconds - row.start_seconds) / Math.max(0.01, row.speed || 1));
      }
    }
    return total;
  }

  function timingDraft(segment: Segment) {
    return segmentDrafts[segment.segment_id] || { start_seconds: segment.start_seconds, end_seconds: segment.end_seconds, speed: segment.speed || 1 };
  }

  function updateTiming(segment: Segment, patch: Partial<{ start_seconds: number; end_seconds: number; speed: number }>) {
    setSegmentDrafts((current) => ({ ...current, [segment.segment_id]: { ...timingDraft(segment), ...patch } }));
  }

  async function saveTiming(segment: Segment) {
    const draft = timingDraft(segment);
    if (draft.start_seconds < 0 || draft.end_seconds <= draft.start_seconds || draft.speed < 0.25 || draft.speed > 4) {
      setMessage("起點、終點或速度不符合規則。");
      return;
    }
    setBusy(`timing:${segment.segment_id}`);
    const result = await api.saveSegmentTiming(detail.project.id, segment.segment_id, draft);
    setBusy("");
    setMessage(result.ok ? "片段時間與速度已儲存，Approval 已失效。" : `片段時間儲存失敗：${result.error || "未知錯誤"}`);
    if (result.ok) await refreshProject({ forceFresh: true });
  }

  async function quickAudio(segmentId: string, role: AudioSegmentSettings["role"] | "default") {
    const segments = { ...detail.audio.segments, [segmentId]: role === "default" ? null : { ...(detail.audio.segments[segmentId] || {}), role } };
    const result = await api.audioSettings(detail.project.id, { segments });
    setMessage(result.ok ? "分鏡片段原音設定已更新，專案已回到待審。" : `原音設定失敗：${result.error || "未知錯誤"}`);
    if (result.ok) await refreshProject({ forceFresh: true });
  }

  async function quickColor(segmentId: string) {
    const patch = colorTogglePatch(detail.color, segmentId);
    const result = await api.colorSettings(detail.project.id, { schema_version: detail.color.schema_version, enabled: detail.color.enabled, applied: detail.color.applied, segments: { [segmentId]: patch } });
    setMessage(result.ok ? "分鏡片段調色設定已更新，專案已回到待審。" : `調色設定失敗：${result.error || "未知錯誤"}`);
    if (result.ok) await refreshProject({ forceFresh: true });
  }

  async function resetColor(segmentId: string) {
    const result = await api.colorSettings(detail.project.id, { schema_version: detail.color.schema_version, enabled: detail.color.enabled, applied: detail.color.applied, segments: { [segmentId]: colorResetPatch() } });
    setMessage(result.ok ? "已恢復此片段的專案調色預設。" : `恢復調色預設失敗：${result.error || "未知錯誤"}`);
    if (result.ok) await refreshProject({ forceFresh: true });
  }

  function effectiveAudio(segment: Segment) {
    const override = detail.audio.segments[segment.segment_id];
    const raw = override?.role || detail.audio.original_audio.default_role || segment.audio_role || "lower";
    const label: Record<string, string> = { keep: "保留原音", keep_original: "保留原音", lower: "降低原音", lower_original: "降低原音", mute: "靜音", bgm_only: "只留 BGM" };
    return override?.role ? (label[raw] || raw) : `使用專案預設（${label[raw] || raw}）`;
  }

  function effectiveColor(segment: Segment) {
    const override = detail.color.segments[segment.segment_id];
    const enabled = detail.color.enabled && (override?.enabled ?? true) && !override?.excluded;
    return override ? (enabled ? "啟用" : "停用") : `使用專案預設（${enabled ? "啟用" : "停用"}）`;
  }

  if (!state.exists && !state.groups.length) return <Card title="分鏡審核"><p className="muted">先建立分鏡，系統會依內容感知與專案類型提出分組建議。</p><button className="good" disabled={busy === "generate"} onClick={() => void generate()}>{busy === "generate" ? "建立中…" : "建立分鏡"}</button></Card>;
  return <Card title="分鏡審核">
    <div className="storyboard-toolbar">
      <div><b>主要操作介面</b><span className="muted">卡片可拖曳到指定卡片前後；↑／↓ 可用鍵盤調整。Lock 只保護自動重建，不限制人工編輯。</span>{isDirty && <strong className="storyboard-dirty">有未儲存變更</strong>}</div>
      <div className="row">
        <button disabled={Boolean(busy) || isDirty} onClick={() => void generate(true)}>重新產生分鏡</button>
        <button disabled={Boolean(busy) || saveInProgress || !isDirty} onClick={() => void save()} className="good">{saveInProgress ? "儲存中…" : "儲存分鏡"}</button>
        <label>起始秒數<input className="storyboard-range-start" type="number" min={0} step={0.1} value={selectedSegmentId ? timelineStart(selectedSegmentId) : rangeStart} onChange={(event) => { setSelectedSegmentId(""); setRangeStart(Math.max(0, Number(event.target.value))); }} /></label>
        <select value={rangeDuration} onChange={(event) => setRangeDuration(Number(event.target.value))}><option value={5}>5 秒</option><option value={8}>8 秒</option><option value={12}>12 秒</option></select>
        <button disabled={Boolean(busy)} onClick={() => void preview("range")}>{selectedSegmentId ? "預覽目前分鏡範圍" : `預覽開頭 ${rangeDuration} 秒`}</button>
        <button disabled={Boolean(busy)} onClick={() => void preview("range", undefined, true)}>強制重新產生</button>
      </div>
    </div>
    {state.summary && <div className="storyboard-summary"><b>共 {state.summary.total_segments} 個片段｜使用 {state.summary.included_segments} 個｜排除 {state.summary.excluded_segments} 個｜預估 {time(state.summary.estimated_duration_seconds)}</b>{state.groups.map((group) => { const item = state.summary?.groups.find((summary) => summary.group_id === group.group_id); return <span key={group.group_id}>{group.title} {item?.count || 0} 段｜{time(item?.duration_seconds || 0)}</span>; })}</div>}
    <div className="row storyboard-group-tools"><input value={newGroup} onChange={(event) => setNewGroup(event.target.value)} placeholder="新增分組名稱" /><button onClick={addGroup}>新增分組</button></div>
    {isDirty && <div className="storyboard-summary muted">摘要會在儲存後更新。</div>}
    <div className="storyboard-groups">
      {state.groups.map((group) => <section className="storyboard-group" key={group.group_id} onDragOver={(event) => event.preventDefault()} onDrop={() => dropOnGroup(group.group_id)}>
        <div className="storyboard-group-heading"><input value={group.title} onChange={(event) => editGroup(group.group_id, event.target.value)} /><span className="muted">{group.category}</span><div className="row"><button aria-label="上移群組" onClick={() => moveGroup(group.group_id, -1)}>↑</button><button aria-label="下移群組" onClick={() => moveGroup(group.group_id, 1)}>↓</button>{!rowsByGroup(group.group_id).length && <button onClick={() => deleteEmptyGroup(group.group_id)}>刪除空群組</button>}</div></div>
        {rowsByGroup(group.group_id).map((segment) => {
          const item = state.segments[segment.segment_id];
          const draft = timingDraft(segment);
          const colorOverride = detail.color.segments[segment.segment_id];
          const colorEnabled = effectiveSegmentColorEnabled(detail.color.enabled, colorOverride);
          return <div key={segment.segment_id} className="storyboard-drop-wrapper">
            <div className={`storyboard-drop-indicator${dropTarget?.segmentId === segment.segment_id && dropTarget.position === "before" ? " active" : ""}`}>放到此片段前</div>
            <article className={`storyboard-card${item?.included ? "" : " excluded"}`} draggable onDragStart={(event) => { setDragged(segment.segment_id); event.dataTransfer.setData("text/plain", segment.segment_id); }} onDragOver={(event) => { event.preventDefault(); const rect = event.currentTarget.getBoundingClientRect(); setDropTarget({ segmentId: segment.segment_id, position: event.clientY < rect.top + rect.height / 2 ? "before" : "after" }); }} onDrop={(event) => { event.stopPropagation(); const target = dropTarget || { segmentId: segment.segment_id, position: "after" as const }; reorder(dragged || event.dataTransfer.getData("text/plain"), target.segmentId, target.position); }} onClick={() => setSelectedSegmentId(segment.segment_id)}>
              <div className="storyboard-thumb">{item?.thumbnail_url ? <img src={item.thumbnail_url} alt={`${segment.title} 代表畫格`} /> : <span>尚未產生代表畫格</span>}</div>
              <div className="storyboard-card-body">
                <div className="row"><b>{segment.title || segment.segment_id}</b><span className="muted">{segment.source_filename || segment.clip_id}</span></div>
                <span className="muted">{time(segment.start_seconds)} ~ {time(segment.end_seconds)}｜成片 {time(Math.max(0, (segment.end_seconds - segment.start_seconds) / Math.max(.01, segment.speed || 1)))}</span>
                <span>Scene Role：{segment.scene_role}｜故事位置：{segment.story_position || "未指定"}｜AI {Number(segment.score || 0).toFixed(2)}｜{segment.suggested_use}</span>
                <span>原音：{effectiveAudio(segment)}｜調色：{colorEnabled ? "啟用" : "停用"}｜{item?.included ? "納入" : "排除"}</span>
                <div className="row">
                  <label className="toggle"><input type="checkbox" checked={Boolean(item?.included)} onChange={(event) => editSegment(segment.segment_id, { included: event.target.checked })} />納入</label>
                  <label className="toggle"><input type="checkbox" checked={Boolean(item?.locked)} onChange={(event) => editSegment(segment.segment_id, { locked: event.target.checked })} />鎖定</label>
                  <select value={item?.group_id || group.group_id} onChange={(event) => moveToGroup(segment.segment_id, event.target.value)}><option value={group.group_id}>{group.title}</option>{state.groups.filter((option) => option.group_id !== group.group_id).map((option) => <option key={option.group_id} value={option.group_id}>{option.title}</option>)}</select>
                  <select value={item?.thumbnail_time_ratio || .5} onChange={(event) => void thumbnail(segment, Number(event.target.value))}><option value="0.25">代表畫格 25%</option><option value="0.5">代表畫格 50%</option><option value="0.75">代表畫格 75%</option></select>
                  <select aria-label="快速原音" value={detail.audio.segments[segment.segment_id]?.role || "default"} onChange={(event) => void quickAudio(segment.segment_id, event.target.value as AudioSegmentSettings["role"] | "default")}><option value="default">套用專案預設</option><option value="keep">保留原音</option><option value="lower">降低原音</option><option value="mute">靜音</option><option value="bgm_only">只留 BGM</option></select>
                  <button onClick={() => void quickColor(segment.segment_id)}>{colorEnabled ? "停用此片段調色" : "啟用此片段調色"}</button>
                  {colorOverride && <button onClick={() => void resetColor(segment.segment_id)}>恢復專案預設</button>}
                </div>
                <div className="row storyboard-timing"><label>起點<input type="number" min={0} step={0.001} value={draft.start_seconds} onChange={(event) => updateTiming(segment, { start_seconds: Number(event.target.value) })} /></label><label>終點<input type="number" min={0} step={0.001} value={draft.end_seconds} onChange={(event) => updateTiming(segment, { end_seconds: Number(event.target.value) })} /></label><label>速度<input type="number" min={0.25} max={4} step={0.05} value={draft.speed} onChange={(event) => updateTiming(segment, { speed: Number(event.target.value) })} /></label><button disabled={busy === `timing:${segment.segment_id}`} onClick={() => void saveTiming(segment)}>{busy === `timing:${segment.segment_id}` ? "儲存中…" : "儲存剪點"}</button><button aria-label="上移片段" onClick={() => moveVertical(segment.segment_id, -1)}>↑</button><button aria-label="下移片段" onClick={() => moveVertical(segment.segment_id, 1)}>↓</button></div>
                <textarea value={item?.notes || ""} onChange={(event) => editSegment(segment.segment_id, { notes: event.target.value })} placeholder="分鏡備註" />
                <div className="row"><button disabled={Boolean(busy)} onClick={() => void thumbnail(segment, item?.thumbnail_time_ratio || .5)}>代表畫格</button><button disabled={Boolean(busy)} onClick={() => void preview("segment", segment.segment_id)}>預覽此片段</button><button disabled={Boolean(busy)} onClick={() => void preview("transition", segment.segment_id)}>預覽前後銜接</button></div>
              </div>
            </article>
            <div className={`storyboard-drop-indicator${dropTarget?.segmentId === segment.segment_id && dropTarget.position === "after" ? " active" : ""}`}>放到此片段後</div>
          </div>;
        })}
      </section>)}
    </div>
    {previewItems.length > 0 && <div className="storyboard-preview-list">{previewItems.map((item, index) => <div key={`${item.kind}-${index}`}><b>{item.kind === "incoming" ? "前段銜接" : item.kind === "outgoing" ? "後段銜接" : "分鏡預覽"}｜{Number(item.duration_seconds || 0).toFixed(1)} 秒</b>{item.url && <video className="storyboard-preview-video" controls src={item.url} />}</div>)}</div>}
  </Card>;
}

function AudioMixingPanel({ detail, bgmTracks, setMessage, refreshProject }: { detail: ProjectDetail; bgmTracks: BgmTrack[]; setMessage: (value: string) => void; refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]> }) {
  const fallback: AudioState = {
    schema_version: 1,
    enabled: true,
    bgm: { bgm_id: null, enabled: false, volume_db: -18, start_seconds: 0, loop: true, fade_in_seconds: 1.5, fade_out_seconds: 2 },
    original_audio: { default_role: "lower", default_volume_db: 0, lower_volume_db: -8, fade_in_seconds: .1, fade_out_seconds: .1 },
    normalization: { enabled: true, target_lufs: -14, true_peak_db: -1 },
    segments: {}
  };
  const [state, setState] = useState<AudioState>(detail.audio || fallback);
  const [previewUrl, setPreviewUrl] = useState("");
  const [previewInfo, setPreviewInfo] = useState<{ cacheHit: boolean; duration: number; start: number; segmentId?: string } | null>(null);
  const [busy, setBusy] = useState("");
  useEffect(() => setState(detail.audio || fallback), [detail.project.id, detail.audio]);
  const patchState = (patch: Partial<AudioState>) => setState((current) => ({ ...current, ...patch }));
  const updateSegment = (segmentId: string, patch: AudioSegmentOverride) => setState((current) => ({ ...current, segments: { ...current.segments, [segmentId]: { ...(current.segments[segmentId] || {}), ...patch } } }));
  const resetSegment = (segmentId: string) => setState((current) => ({ ...current, segments: { ...current.segments, [segmentId]: null } }));
  const effectiveSegment = (segment: Segment): AudioSegmentSettings => {
    const configured = state.segments[segment.segment_id];
    const override = configured && typeof configured === "object" ? configured : {};
    const role = override.role || state.original_audio.default_role || (segment.audio_role === "keep_original" ? "keep" : segment.audio_role === "mute" ? "mute" : "lower");
    const defaultVolume = role === "lower" ? state.original_audio.lower_volume_db : state.original_audio.default_volume_db;
    return {
      role,
      volume_db: override.volume_db ?? (role === "mute" || role === "bgm_only" ? 0 : defaultVolume),
      fade_in_seconds: override.fade_in_seconds ?? (state.original_audio.fade_in_seconds ?? .1),
      fade_out_seconds: override.fade_out_seconds ?? (state.original_audio.fade_out_seconds ?? .1),
      locked: Boolean(override.locked),
    };
  };
  async function save() {
    setBusy("save");
    const result = await api.audioSettings(detail.project.id, { enabled: state.enabled, bgm: state.bgm, original_audio: state.original_audio, normalization: state.normalization, segments: state.segments });
    setBusy("");
    if (!result.ok) { setMessage(`音訊設定儲存失敗：${result.error || "未知錯誤"}`); return; }
    if (result.state) setState(result.state);
    setMessage("音訊設定已儲存，專案已回到待審。");
    await refreshProject();
  }
  async function preview(segmentId = "", force = false) {
    setBusy("preview");
    const result = await api.audioPreview(detail.project.id, {
      segmentId,
      durationSeconds: 12,
      patch: { enabled: state.enabled, bgm: state.bgm, original_audio: state.original_audio, normalization: state.normalization, segments: state.segments },
      force,
    });
    setBusy("");
    if (!result.ok) { setMessage(`音訊預覽失敗：${result.error || "未知錯誤"}`); return; }
    setPreviewUrl(result.url || "");
    setPreviewInfo({ cacheHit: Boolean(result.cache_hit), duration: Number(result.duration_seconds || 0), start: Number(result.timeline_start_seconds || 0), segmentId: segmentId || undefined });
    setMessage(force ? "音訊預覽已強制重新產生。保存後仍需重新核准才能正式輸出。" : result.cache_hit ? "音訊預覽已從快取載入。" : "音訊預覽完成。保存後仍需重新核准才能正式輸出。");
  }
  return <div className="grid">
    <Card title="音訊混音與 BGM">
      <label className="toggle"><input type="checkbox" checked={state.enabled} onChange={(e) => patchState({ enabled: e.target.checked })} /> 啟用專案音訊設定</label>
      <div className="audio-grid">
        <label>BGM<select value={state.bgm.bgm_id ?? ""} onChange={(e) => patchState({ bgm: { ...state.bgm, bgm_id: e.target.value ? Number(e.target.value) : null, enabled: Boolean(e.target.value) } })}><option value="">不使用</option>{bgmTracks.map((track) => <option key={track.id} value={track.id}>{track.title}</option>)}</select></label>
        <label>音量 dB<input type="number" min={-60} max={12} step={1} value={state.bgm.volume_db} onChange={(e) => patchState({ bgm: { ...state.bgm, volume_db: Number(e.target.value) } })} /></label>
        <label>起始秒數<input type="number" min={0} step={0.1} value={state.bgm.start_seconds} onChange={(e) => patchState({ bgm: { ...state.bgm, start_seconds: Number(e.target.value) } })} /></label>
        <label>淡入秒數<input type="number" min={0} step={0.1} value={state.bgm.fade_in_seconds} onChange={(e) => patchState({ bgm: { ...state.bgm, fade_in_seconds: Number(e.target.value) } })} /></label>
        <label>淡出秒數<input type="number" min={0} step={0.1} value={state.bgm.fade_out_seconds} onChange={(e) => patchState({ bgm: { ...state.bgm, fade_out_seconds: Number(e.target.value) } })} /></label>
        <label className="toggle"><input type="checkbox" checked={state.bgm.loop} onChange={(e) => patchState({ bgm: { ...state.bgm, loop: e.target.checked } })} /> BGM 循環</label>
      </div>
      <div className="audio-grid">
        <label>原音預設角色<select value={state.original_audio.default_role} onChange={(e) => patchState({ original_audio: { ...state.original_audio, default_role: e.target.value as AudioSegmentSettings["role"] } })}><option value="keep">保留原音</option><option value="lower">降低原音</option><option value="mute">靜音</option><option value="bgm_only">只留 BGM</option></select></label>
        <label>降低原音 dB<input type="number" min={-60} max={12} step={1} value={state.original_audio.lower_volume_db} onChange={(e) => patchState({ original_audio: { ...state.original_audio, lower_volume_db: Number(e.target.value) } })} /></label>
        <label className="toggle"><input type="checkbox" checked={state.normalization.enabled} onChange={(e) => patchState({ normalization: { ...state.normalization, enabled: e.target.checked } })} /> 音量正規化</label>
        <label>目標 LUFS<input type="number" min={-40} max={0} step={1} value={state.normalization.target_lufs} onChange={(e) => patchState({ normalization: { ...state.normalization, target_lufs: Number(e.target.value) } })} /></label>
        <label>True Peak dB<input type="number" min={-20} max={0} step={0.1} value={state.normalization.true_peak_db} onChange={(e) => patchState({ normalization: { ...state.normalization, true_peak_db: Number(e.target.value) } })} /></label>
      </div>
      <div className="row"><button className="good" disabled={busy === "save"} onClick={save}>{busy === "save" ? "儲存中…" : "儲存音訊設定"}</button><button disabled={Boolean(busy)} onClick={() => preview()}>{busy === "preview" ? "預覽產生中…" : "產生 12 秒預覽"}</button><button disabled={Boolean(busy)} onClick={() => preview("", true)}>強制重新產生</button></div>
      {previewInfo && <p className="muted">預覽範圍 {previewInfo.start.toFixed(1)}s，長度 {previewInfo.duration.toFixed(1)}s，{previewInfo.cacheHit ? "命中快取" : "新產生"}</p>}
      {previewUrl && <video controls width="100%" src={previewUrl} />}
      {state.bgm.track && <p className="muted">目前 BGM：{state.bgm.track.title}｜作者：{state.bgm.track.artist || "未知"}｜{state.bgm.track.duration_seconds ? `${state.bgm.track.duration_seconds}s` : "長度未知"}｜授權：{state.bgm.track.license_name || "未填"}｜{state.bgm.track.attribution_text || "未填署名"}</p>}
      {state.enabled && (!state.bgm.enabled || !state.bgm.bgm_id) && (state.original_audio.default_role === "bgm_only" || Object.values(state.segments).some((item) => item?.role === "bgm_only")) && <div className="notice">有片段設定為只留 BGM，但目前沒有有效 BGM；儲存後的 Manifest／預覽會阻擋，請先選擇可讀取的音樂。</div>}
    </Card>
    <Card title="片段原音角色">
      {detail.segments.map((segment) => { const item = effectiveSegment(segment); const customized = Boolean(state.segments[segment.segment_id]); return <div className="audio-segment" key={segment.segment_id}><b>{segment.title || segment.segment_id}</b><span className="muted">{customized ? "自訂設定" : "使用專案預設"}</span><select value={item.role} onChange={(e) => updateSegment(segment.segment_id, { role: e.target.value as AudioSegmentSettings["role"] })}><option value="keep">保留</option><option value="lower">降低</option><option value="mute">靜音</option><option value="bgm_only">只留 BGM</option></select><input aria-label="片段音量" type="number" min={-60} max={12} step={1} value={item.volume_db} onChange={(e) => updateSegment(segment.segment_id, { volume_db: Number(e.target.value) })} /><input aria-label="淡入秒數" type="number" min={0} step={.1} value={item.fade_in_seconds} onChange={(e) => updateSegment(segment.segment_id, { fade_in_seconds: Number(e.target.value) })} /><input aria-label="淡出秒數" type="number" min={0} step={.1} value={item.fade_out_seconds} onChange={(e) => updateSegment(segment.segment_id, { fade_out_seconds: Number(e.target.value) })} /><label className="toggle"><input type="checkbox" checked={item.locked} onChange={(e) => updateSegment(segment.segment_id, { locked: e.target.checked })} />鎖定</label><button onClick={() => resetSegment(segment.segment_id)}>套用專案預設</button><button disabled={Boolean(busy)} onClick={() => preview(segment.segment_id)}>預覽</button><button disabled={Boolean(busy)} onClick={() => preview(segment.segment_id, true)}>強制重跑</button><span>dB｜淡入｜淡出</span></div>; })}
      {!detail.segments.length && <p>尚未有可調整的片段。</p>}
    </Card>
  </div>;
}

function ClipSummary({ projectId, clip, setMessage, refreshProject }: { projectId: number; clip: { video_id: number; visual_summary: string }; setMessage: (value: string) => void; refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]> }) {
  const [text, setText] = useState(clip.visual_summary || "");
  useEffect(() => setText(clip.visual_summary || ""), [clip.visual_summary]);
  async function save() {
    setMessage("正在儲存內容感知描述...");
    const result = await api.saveClipSummary(projectId, clip.video_id, text);
    setMessage(result.ok ? "內容感知描述已儲存，專案已回到待審。" : "儲存失敗：找不到素材");
    await refreshProject();
  }
  return <div className="stack"><textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="內容感知描述" /><button onClick={save}>儲存描述</button></div>;
}

function ColorConsistencyPanel({ detail, setMessage, refreshProject }: { detail: ProjectDetail; setMessage: (value: string) => void; refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]> }) {
  const [state, setState] = useState<ColorState>(() => detail.color || emptyColorState());
  const [previews, setPreviews] = useState<Array<{ video_id: number; segment_id: string; before_url: string; after_url: string; cache_hit: boolean }>>([]);
  const [busy, setBusy] = useState("");
  useEffect(() => setState(detail.color || emptyColorState()), [detail.project.id, detail.color]);

  const selectedReferenceId = typeof state.reference === "object" && "id" in state.reference ? state.reference.id : "";
  const suggested = state.suggested;
  const applied = state.applied;

  async function analyze(force = false) {
    setBusy("analyze");
    setMessage(force ? "正在重跑色彩分析..." : "正在分析核心畫面色彩...");
    try {
      const result = await api.colorAnalyze(detail.project.id, force);
      if (!result.ok || !result.state) {
        setMessage(`色彩分析失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setState(result.state);
      setMessage("色彩分析完成，請確認建議值後再儲存實際套用值。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`色彩分析失敗：${error instanceof Error ? error.message : "網路或服務錯誤"}`);
    } finally {
      setBusy("");
    }
  }

  async function save() {
    setBusy("save");
    try {
      const result = await api.colorSettings(detail.project.id, toColorStatePatch(state));
      if (!result.ok || !result.state) {
        setMessage(`色彩設定儲存失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setState(result.state);
      setMessage("色彩設定已儲存，專案已回到待審。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`色彩設定儲存失敗：${error instanceof Error ? error.message : "網路或服務錯誤"}`);
    } finally {
      setBusy("");
    }
  }

  async function changeReference(referenceId: string) {
    if (!referenceId) return;
    setBusy("reference");
    try {
      const result = await api.colorReference(detail.project.id, referenceId);
      if (!result.ok || !result.state) {
        setMessage(`色彩基準更新失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setState(result.state);
      setMessage("色彩基準已更新，請重新確認建議值。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`色彩基準更新失敗：${error instanceof Error ? error.message : "網路或服務錯誤"}`);
    } finally {
      setBusy("");
    }
  }

  async function preview(force = false) {
    setBusy("preview");
    setMessage(force ? "正在強制重新產生 Before / After 調色預覽..." : "正在產生 Before / After 調色預覽...");
    try {
      const result = await api.colorPreviewDirect(detail.project.id, force);
      if (!result.ok) {
        setPreviews([]);
        setMessage(`調色預覽失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setPreviews(result.previews || []);
      setMessage("Before / After 調色預覽已完成。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setPreviews([]);
      setMessage(`調色預覽失敗：${error instanceof Error ? error.message : "網路或服務錯誤"}`);
    } finally {
      setBusy("");
    }
  }

  function updateApplied(field: keyof ColorAdjustment, value: string | number) {
    setState({ ...state, applied: { ...state.applied, [field]: field === "mode" || field === "lut_path" || field === "lut_kind" ? String(value) : Number(value) } });
  }

  function updateSegment(segmentId: string, patch: { enabled?: boolean; locked?: boolean; excluded?: boolean }) {
    const current = state.segments[segmentId] || { enabled: true, locked: false, excluded: false };
    setState({ ...state, segments: { ...state.segments, [segmentId]: { ...current, ...patch } } });
  }

  function segmentState(segmentId: string): ColorSegmentState {
    return state.segments[segmentId] || { enabled: true, locked: false, excluded: false, suggested: state.suggested, applied: state.applied, confidence: 0, warnings: [] };
  }

  function applySegmentSuggestion(segmentId: string) {
    const current = segmentState(segmentId);
    setState({ ...state, segments: { ...state.segments, [segmentId]: { ...current, applied: { ...(current.suggested || state.suggested) } } } });
  }

  function resetSegment(segmentId: string) {
    const current = segmentState(segmentId);
    setState({ ...state, segments: { ...state.segments, [segmentId]: { ...current, applied: { ...state.applied } } } });
  }

  function updateSegmentApplied(segmentId: string, field: keyof ColorAdjustment, value: string | number) {
    const current = segmentState(segmentId);
    const appliedValues = { ...(current.applied || state.applied), [field]: Number(value) };
    setState({ ...state, segments: { ...state.segments, [segmentId]: { ...current, applied: appliedValues } } });
  }

  return (
    <Card title="色彩一致性與調色預覽">
      <div className="color-toolbar row">
        <button onClick={() => analyze(false)} disabled={Boolean(busy)}>分析核心畫面</button>
        <button onClick={() => analyze(true)} disabled={Boolean(busy)}>重跑色彩分析</button>
        <button onClick={() => void preview(false)} disabled={Boolean(busy)}>產生 Before / After 預覽</button>
        <button onClick={() => void preview(true)} disabled={Boolean(busy)}>強制重新產生</button>
        <button className="good" onClick={save} disabled={busy !== ""}>儲存實際套用值</button>
        {busy && <span className="muted">{busy === "preview" ? "正在輸出預覽..." : "處理中..."}</span>}
      </div>
      <label className="toggle"><input type="checkbox" checked={state.enabled} onChange={(e) => setState({ ...state, enabled: e.target.checked })} /> 啟用專案色彩一致性</label>
      <div className="color-grid">
        <div>
          <label>Reference Clip / Frame</label>
          <select value={selectedReferenceId} onChange={(e) => void changeReference(e.target.value)} disabled={!state.references.length || Boolean(busy)}>
            <option value="">尚未選擇基準畫面</option>
            {state.references.map((reference) => <option key={reference.id} value={reference.id}>{reference.type === "segment" ? "片段" : "畫格"}｜{reference.label || "未命名"}｜{reference.score.toFixed(2)}</option>)}
          </select>
          <p className="muted">{state.analysis.basis_text || "先執行色彩分析，系統會從內容感知結果挑選核心畫面。"}</p>
          {typeof state.reference === "object" && "frame_url" in state.reference && state.reference.frame_url ? <img className="reference-thumbnail" src={state.reference.frame_url} alt="色彩基準畫面" /> : null}
          {state.analysis.luma && <p className="muted">平均亮度 {Number(state.analysis.luma.average || 0).toFixed(1)}｜高光比例 {(Number(state.analysis.luma.highlight_ratio || 0) * 100).toFixed(1)}%｜信心 {state.analysis.confidence || "未分析"}</p>}
          {state.analysis.warnings?.length ? <p className="job-error">{state.analysis.warnings.join("｜")}</p> : null}
        </div>
        <div>
          <label>技術 LUT</label>
          <select value={applied.mode} onChange={(e) => updateApplied("mode", e.target.value)}>
            <option value="dji_dlog_m">DJI D-Log M</option>
            <option value="dji_dlog">DJI D-Log</option>
            <option value="dji_lut">自訂 DJI LUT</option>
            <option value="safe_restore">保守修正</option>
            <option value="manual">手動調整</option>
            <option value="none">不套用</option>
          </select>
          <input value={applied.lut_path} onChange={(e) => updateApplied("lut_path", e.target.value)} placeholder=".cube LUT 路徑" />
        </div>
      </div>
      <div className="adjustment-grid">
        {(["exposure", "temperature", "tint", "contrast", "highlights", "shadows", "saturation", "gamma"] as const).map((field) => (
          <label key={field}>{adjustmentLabel(field)}<input type="number" step="0.01" value={applied[field]} onChange={(e) => updateApplied(field, e.target.value)} /></label>
        ))}
      </div>
      <div className="suggested-box">
        <b>系統建議值（唯讀）</b>
        <span>曝光 {suggested.exposure}｜色溫 {suggested.temperature}｜色調 {suggested.tint}｜對比 {suggested.contrast}｜飽和 {suggested.saturation}｜Gamma {suggested.gamma}</span>
      </div>
      {detail.segments.length > 0 && <div className="color-segments"><b>片段覆寫</b>{detail.segments.map((segment) => { const item = segmentState(segment.segment_id); const appliedDisabled = item.excluded || !item.enabled; return <div className="color-segment-row" key={segment.segment_id}><span><b>{segment.clip_id}</b>｜{segment.title}<small>信心 {Number(item.confidence || 0).toFixed(2)}{item.warnings?.length ? `｜${item.warnings.join("、")}` : ""}{appliedDisabled ? "｜目前不套用片段色彩" : ""}</small></span><label><input type="checkbox" checked={item.enabled} onChange={(e) => updateSegment(segment.segment_id, { enabled: e.target.checked })} /> 啟用</label><label><input type="checkbox" checked={item.locked} onChange={(e) => updateSegment(segment.segment_id, { locked: e.target.checked })} /> Lock</label><label><input type="checkbox" checked={item.excluded} onChange={(e) => updateSegment(segment.segment_id, { excluded: e.target.checked })} /> Exclude</label><button disabled={appliedDisabled} onClick={() => applySegmentSuggestion(segment.segment_id)}>套用建議</button><button disabled={appliedDisabled} onClick={() => resetSegment(segment.segment_id)}>重設為專案</button><details><summary>片段色彩值</summary><div className="adjustment-grid">{(["exposure", "temperature", "tint", "contrast", "highlights", "shadows", "saturation", "gamma"] as const).map((field) => <label key={field}>{adjustmentLabel(field)}<input disabled={appliedDisabled} type="number" step="0.01" value={item.applied?.[field] ?? state.applied[field]} onChange={(e) => updateSegmentApplied(segment.segment_id, field, e.target.value)} /></label>)}</div></details></div>; })}</div>}
      {previews.length > 0 && <div className="preview-grid"><b>最近一次預覽</b>{previews.map((previewItem) => <div className="preview-item" key={`${previewItem.video_id}-${previewItem.segment_id}`}><span>素材 #{previewItem.video_id}｜{previewItem.segment_id} {previewItem.cache_hit ? "（快取）" : "（重新產生）"}</span><div className="preview-videos"><div><small>Before</small><video controls preload="metadata" src={previewItem.before_url} /></div><div><small>After</small><video controls preload="metadata" src={previewItem.after_url} /></div></div></div>)}</div>}
    </Card>
  );
}

function emptyColorState(): ColorState {
  return {
    schema_version: 2,
    enabled: true,
    reference: {},
    references: [],
    analysis: {},
    suggested: { mode: "none", lut_path: "", lut_kind: "", exposure: 0, temperature: 0, tint: 0, contrast: 1, saturation: 1, gamma: 1, highlights: 0, shadows: 0 },
    applied: { mode: "none", lut_path: "", lut_kind: "", exposure: 0, temperature: 0, tint: 0, contrast: 1, saturation: 1, gamma: 1, highlights: 0, shadows: 0 },
    segments: {},
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
      ...(segment.applied ? { applied: { ...segment.applied } } : {})
    }]))
  };
}

function adjustmentLabel(field: keyof ColorAdjustment) {
  return ({ exposure: "曝光", temperature: "白平衡色溫", tint: "白平衡色調", contrast: "對比", highlights: "高光", shadows: "陰影", saturation: "飽和度", gamma: "Gamma" } as Record<string, string>)[field] || field;
}

function Workflow({ detail, active, onNavigate }: { detail: ProjectDetail; active: Workspace; onNavigate: (workspace: Workspace) => void }) {
  const perceptionDone = detail.clips.some((clip) => clip.segment_count > 0 || clip.status === "perceived");
  const storyDone = detail.segments.length > 0 || Boolean(detail.script);
  const outputDone = detail.workflow.stages.some((stage) => ["done", "completed", "succeeded"].includes(stage.status));
  const steps: Array<{ label: string; workspace?: Workspace; done: boolean; active?: boolean }> = [
    { label: "匯入素材", workspace: "assets", done: detail.clips.length > 0 },
    { label: "內容感知", workspace: "assets", done: perceptionDone },
    { label: "故事整理", workspace: "storyboard", done: storyDone },
    { label: "分鏡審核", workspace: "storyboard", done: detail.segments.length > 0, active: active === "storyboard" },
    { label: "調色與音訊", workspace: "color", done: Boolean(detail.color?.analysis?.basis_text), active: active === "color" || active === "audio" },
    { label: "核准", workspace: "output", done: detail.can_render, active: active === "output" && !detail.can_render },
    { label: "輸出", workspace: "output", done: outputDone, active: active === "output" && detail.can_render },
  ];
  return <div className="workflow-stepper" aria-label="專案工作流">{steps.map((step, index) => <div className="workflow-step-wrap" key={step.label}><button className={`workflow-step${step.done ? " done" : ""}${step.active ? " current" : ""}`} onClick={() => step.workspace && onNavigate(step.workspace)}><span className="workflow-number">{step.done ? "✓" : index + 1}</span><b>{step.label}</b></button>{index < steps.length - 1 && <span className={`workflow-connector${step.done ? " done" : ""}`} />}</div>)}</div>;
}

function WorkflowSkeleton({ detail }: { detail: ProjectDetail }) {
  return (
    <Card title="OpenMontage-style 工作流骨架">
      <div className="workflow-grid">
        {detail.workflow.stages.map((stage) => (
          <div className="workflow-card" key={stage.id}>
            <b>{stage.label}</b>
            <span className={stage.status === "done" ? "pill ok" : "pill"}>{stage.status}</span>
            <small>{stage.artifacts.join("\n")}</small>
          </div>
        ))}
      </div>
    </Card>
  );
}

function SegmentTable({ detail }: { detail: ProjectDetail }) {
  const [rows, setRows] = useState<Segment[]>(detail.segments || []);
  const [saved, setSaved] = useState("");
  useEffect(() => setRows(detail.segments || []), [detail.project.id, detail.segments]);
  if (!detail.segments?.length) return <Card title="片段審核"><p>尚未有推薦片段。</p></Card>;
  async function save() {
    await api.saveSegments(detail.project.id, rows.map((row, index) => ({ ...row, manual_order: index + 1 })));
    setSaved("已儲存，專案已回到待審");
  }
  return (
    <Card title="片段審核">
      <div className="row"><button onClick={save}>儲存片段審核</button>{saved && <span className="muted">{saved}</span>}</div>
      <div className="table">
        <div className="thead">
          <span>使用</span><span>順序</span><span>片段</span><span>時間</span><span>場景</span><span>用途</span><span>分數</span><span>備註</span>
        </div>
        {rows.map((s, index) => (
          <div className="trow" key={s.segment_id}>
            <span><input type="checkbox" checked={s.include} onChange={(e) => update(index, { include: e.target.checked })} /> {s.include ? "保留" : "不用"}</span>
            <span className="order-edit">
              <button disabled={index === 0} onClick={() => move(index, -1)}>↑</button>
              <button disabled={index === rows.length - 1} onClick={() => move(index, 1)}>↓</button>
              {index + 1}
            </span>
            <span><b>{s.clip_id}</b> {segmentTitle(s.title)}</span>
            <span className="time-edit">
              <TimeBox label="開始" seconds={s.start_seconds} onChange={(value) => update(index, { start_seconds: value })} />
              <b>~</b>
              <TimeBox label="結束" seconds={s.end_seconds} onChange={(value) => update(index, { end_seconds: value })} />
            </span>
            <span>{s.scene_role}</span>
            <span>{useLabel(s.suggested_use)}</span>
            <span>{Number(s.score || 0).toFixed(2)}</span>
            <span><input value={s.user_notes || ""} onChange={(e) => update(index, { user_notes: e.target.value })} /></span>
          </div>
        ))}
      </div>
    </Card>
  );

  function update(index: number, patch: Partial<Segment>) {
    setRows(rows.map((row, i) => i === index ? { ...row, ...patch } : row));
    setSaved("");
  }

  function move(index: number, delta: number) {
    const next = [...rows];
    const target = index + delta;
    [next[index], next[target]] = [next[target], next[index]];
    setRows(next);
    setSaved("");
  }
}

function TimeBox({ label, seconds, onChange }: { label: string; seconds: number; onChange: (value: number) => void }) {
  const value = Math.max(0, Math.round(seconds || 0));
  const minutes = Math.floor(value / 60);
  const secs = value % 60;
  return (
    <input
      aria-label={label}
      title={label}
      value={`${minutes}:${String(secs).padStart(2, "0")}`}
      onChange={(e) => onChange(parseTime(e.target.value))}
    />
  );
}

function time(seconds: number) {
  const value = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(value / 3600);
  const m = Math.floor((value % 3600) / 60);
  const s = String(value % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

function parseTime(value: string) {
  const zh = value.trim().match(/^(\d+)\s*分\s*(\d+)\s*秒?$/);
  if (zh) return Number(zh[1]) * 60 + Number(zh[2]);
  const parts = value.trim().split(":").map(Number);
  if (parts.some(Number.isNaN)) return 0;
  if (parts.length === 3) return Math.max(0, parts[0] * 3600 + parts[1] * 60 + parts[2]);
  if (parts.length === 2) return Math.max(0, parts[0] * 60 + parts[1]);
  return Math.max(0, Number(value) || 0);
}

function segmentTitle(value: string) {
  return value
    .replace("Shorts candidate", "短影音候選片段")
    .replace("B-roll candidate", "補畫面候選片段")
    .replace("Product closeup candidate", "產品特寫候選片段");
}

function useLabel(value: string) {
  return ({ Shorts: "短影音", "B-roll": "補畫面", "Product closeup": "產品特寫" } as Record<string, string>)[value] || value;
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return <div className="card"><h3>{title}</h3>{children}</div>;
}

function Status({ value }: { value: string }) {
  return <span className={value === "approved" ? "pill ok" : "pill"}>{value}</span>;
}

const rootElement = document.getElementById("root");
if (rootElement) createRoot(rootElement).render(<StrictMode><App /></StrictMode>);
