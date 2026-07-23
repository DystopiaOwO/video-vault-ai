import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, ReactNode } from "react";
import { api, type BgmTrack, type Job, type Project, type ProjectDetail } from "./api";
import { RenderJobPanel } from "./components/render/RenderJobPanel";
import { type ProjectDataLoadOptions, ProjectDataLoader } from "./projectDataLoader";
import { AudioMixingWorkspace } from "./workspaces/audio/AudioMixingWorkspace";
import { ColorConsistencyWorkspace } from "./workspaces/color/ColorConsistencyWorkspace";
import { StoryboardWorkspaceController } from "./workspaces/storyboard/StoryboardWorkspaceController";

export function App() {
  if (window.location.pathname === "/bgm") return <BgmPage />;
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [projectQuery, setProjectQuery] = useState("");
  const [currentId, setCurrentId] = useState<number>(0);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [bgmTracks, setBgmTracks] = useState<BgmTrack[]>([]);
  const [notes, setNotes] = useState("");
  const [message, setMessage] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const currentIdRef = useRef(0);
  const mountedRef = useRef(true);
  const loaderRef = useRef<ProjectDataLoader | null>(null);
  const normalizedProjectQuery = projectQuery.trim().toLocaleLowerCase();
  const filteredProjects = useMemo(() => projects.filter((project) => {
    if (!normalizedProjectQuery) return true;
    return [project.name, project.status, String(project.id)]
      .some((value) => String(value || "").toLocaleLowerCase().includes(normalizedProjectQuery));
  }), [normalizedProjectQuery, projects]);

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
    void loadProjects();
    api.bgm().then(setBgmTracks).catch((error) => setMessage(`BGM 載入失敗：${error instanceof Error ? error.message : "未知錯誤"}`));
  }, []);

  async function loadProjects() {
    setProjectsLoading(true);
    try {
      const rows = await api.projects();
      setProjects(rows);
      setCurrentId((id) => id || rows[0]?.id || 0);
      return rows;
    } catch (error) {
      setMessage(`專案載入失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
      return [];
    } finally {
      setProjectsLoading(false);
    }
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
    try {
      action === "approve" ? await api.approve(detail.project.id, notes) : await api.reject(detail.project.id, notes);
      setNotes("");
      setMessage(action === "approve" ? "已核准專案" : "已退回修改");
      setDetail(await api.project(detail.project.id));
    } catch (error) {
      setMessage(`審核操作失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  async function revise() {
    if (!detail) return;
    setMessage("正在依備註重建故事...");
    try {
      await api.revise(detail.project.id, notes);
      setMessage("故事整理已依備註重建");
      setDetail(await api.project(detail.project.id));
    } catch (error) {
      setMessage(`故事重建失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  async function createProject() {
    const name = newProjectName.trim();
    if (!name) {
      setMessage("請先輸入專案名稱。");
      return;
    }
    if (projects.some((project) => project.name.trim().toLocaleLowerCase() === name.toLocaleLowerCase())) {
      setMessage("已有同名專案，請使用不同名稱。");
      return;
    }
    setCreatingProject(true);
    setMessage("正在建立專案...");
    try {
      const result = await api.createProject(name);
      setNewProjectName("");
      await loadProjects();
      selectProject(result.id);
      setMessage("專案已建立，下一步請匯入素材。");
    } catch (error) {
      setMessage(`專案建立失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally {
      setCreatingProject(false);
    }
  }

  function selectProject(projectId: number) {
    if (projectId === currentId) return;
    loaderRef.current?.invalidate();
    setCurrentId(projectId);
    setDetail(null);
    setJobs([]);
    setNotes("");
    setMessage("");
  }

  async function refreshProject(projectId: number, options: ProjectDataLoadOptions = {}): Promise<Job[]> {
    return loadProjectData(projectId, options);
  }

  const projectContent = projectsLoading
    ? <WorkspaceLoading title="正在載入專案" detail="取得專案清單與工作狀態…" />
    : projects.length === 0
      ? <WorkspaceEmpty title="尚未建立專案" detail="從左側輸入名稱建立第一個專案，再匯入多支影片素材。" />
      : !detail || detail.project.id !== currentId
        ? <WorkspaceLoading title="正在開啟專案" detail="載入素材、分鏡、調色、音訊與輸出狀態…" />
        : <ProjectView key={detail.project.id} detail={detail} jobs={jobs} bgmTracks={bgmTracks} notes={notes} setNotes={setNotes} setMessage={setMessage} refreshProject={refreshProject} review={review} revise={revise} />;

  return <main>
    <aside>
      <h1>Video Vault AI</h1>
      <nav className="sidebar-links" aria-label="主要導覽">
        <a className="nav" href="/bgm">BGM 資料庫</a>
        <a className="nav" href="/classic-bgm">舊版 BGM 上傳</a>
        <a className="nav" href="/classic">舊版工作台</a>
      </nav>
      <div className="new-project">
        <label htmlFor="new-project-name">建立專案</label>
        <input id="new-project-name" value={newProjectName} onChange={(event) => setNewProjectName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void createProject(); }} placeholder="例如：福岡旅行 2026" maxLength={80} />
        <button disabled={creatingProject || !newProjectName.trim()} onClick={() => void createProject()}>{creatingProject ? "建立中…" : "新增專案"}</button>
      </div>
      <div className="sidebar-section-heading">
        <h2>專案</h2>
        <span>{filteredProjects.length} / {projects.length}</span>
      </div>
      <label className="project-search">
        <span>搜尋專案</span>
        <input type="search" aria-label="搜尋專案" value={projectQuery} onChange={(event) => setProjectQuery(event.target.value)} placeholder="名稱、狀態或編號" />
      </label>
      <div className="project-list" aria-label="專案清單">
        {projectsLoading && <div className="sidebar-empty">載入專案中…</div>}
        {!projectsLoading && filteredProjects.map((project) => <button key={project.id} className={project.id === currentId ? "project active" : "project"} aria-current={project.id === currentId ? "page" : undefined} onClick={() => selectProject(project.id)}>
          <b>{project.name}</b>
          <span>#{project.id} · {projectStatusLabel(project.status)} · {project.video_count ?? 0} 支素材</span>
        </button>)}
        {!projectsLoading && projects.length > 0 && filteredProjects.length === 0 && <div className="sidebar-empty">
          <b>找不到符合的專案</b>
          <button type="button" onClick={() => setProjectQuery("")}>清除搜尋</button>
        </div>}
        {!projectsLoading && projects.length === 0 && <div className="sidebar-empty">建立第一個專案後會顯示在這裡。</div>}
      </div>
    </aside>
    <section>
      {message && <div className="notice" role="status"><span>{message}</span><button type="button" aria-label="關閉通知" onClick={() => setMessage("")}>×</button></div>}
      {projectContent}
    </section>
  </main>;
}

function BgmPage() {
  const [tracks, setTracks] = useState<BgmTrack[]>([]);
  useEffect(() => {
    api.bgm().then(setTracks);
  }, []);
  return <main>
    <aside>
      <h1>BGM 資料庫</h1>
      <a className="nav" href="/">專案工作台</a>
      <a className="nav" href="/classic-bgm">上傳 BGM</a>
    </aside>
    <section>
      <div className="hero"><div><h2>本地 BGM 總覽</h2><p>{tracks.length} 首可用音樂</p></div></div>
      <div className="grid">
        {tracks.map((track) => <Card key={track.id} title={track.title}>
          <p>{track.artist || "未知作者"} | {track.license_name || "未填授權"} | {track.mood || "未分類"}</p>
          {track.source_url && <p><a href={track.source_url} target="_blank">來源</a></p>}
          <pre>{track.attribution_text || "尚未填寫 YouTube 署名文字。"}</pre>
        </Card>)}
        {!tracks.length && <Card title="尚無 BGM"><p>請先到舊版 BGM 上傳頁登錄本地音樂。</p></Card>}
      </div>
    </section>
  </main>;
}

function ProjectView({ detail, jobs, bgmTracks, notes, setNotes, setMessage, refreshProject, review, revise }: {
  detail: ProjectDetail;
  jobs: Job[];
  bgmTracks: BgmTrack[];
  notes: string;
  setNotes: (value: string) => void;
  setMessage: (value: string) => void;
  refreshProject: (projectId: number, options?: ProjectDataLoadOptions) => Promise<Job[]>;
  review: (action: "approve" | "reject") => void;
  revise: () => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const refreshCurrentProject = (options: ProjectDataLoadOptions = {}) => refreshProject(detail.project.id, options);
  const includedSegments = detail.storyboard?.segments
    ? Object.values(detail.storyboard.segments).filter((segment) => segment.included).length
    : detail.segments.filter((segment) => segment.include !== false).length;

  return <>
    <div className="hero">
      <div>
        <h2>{detail.project.name}</h2>
        <p>{detail.folder || `專案 #${detail.project.id}`}</p>
      </div>
      <Status value={detail.project.status} />
    </div>
    <div className="project-metrics" aria-label="專案摘要">
      <div><span>素材</span><b>{detail.clips.length}</b></div>
      <div><span>感知片段</span><b>{detail.segments.length}</b></div>
      <div><span>納入成片</span><b>{includedSegments}</b></div>
      <div><span>正式輸出</span><b>{detail.can_render ? "已解鎖" : "待核准"}</b></div>
    </div>
    <WorkspaceNavigation />

    <div className="workspace-section" id="workspace-overview" tabIndex={-1}>
      <RenderJobPanel jobs={jobs} projectId={detail.project.id} setMessage={setMessage} refreshProject={refreshCurrentProject} />
      <Workflow detail={detail} />
    </div>

    <div className="workspace-section" id="workspace-storyboard" tabIndex={-1}>
      <StoryboardWorkspaceController detail={detail} setMessage={setMessage} refreshProject={refreshCurrentProject} />
    </div>

    <div className="workspace-section" id="workspace-review" tabIndex={-1}>
      <div className="grid">
        <Card title="審核">
          <p>Gate：{detail.can_render ? "可正式輸出" : detail.render_gate_reason}</p>
          <textarea value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="記錄核准理由、退回項目或重建故事需求" />
          <div className="row">
            <button className="good" onClick={() => review("approve")}>核准專案</button>
            <button className="danger" onClick={() => review("reject")}>退回修改</button>
            <button disabled={!notes.trim()} onClick={revise}>依備註重建故事</button>
          </div>
        </Card>
        <Card title="輸出">
          <div className="output-actions">
            <button onClick={() => void exportProject("hyperframes")}>產生初剪專案</button>
            <button disabled={!detail.can_render} onClick={() => void exportProject("hyperframes-render")}>快速輸出 MP4</button>
            <button className="good" disabled={submitting || !detail.can_render || jobs.some((job) => Boolean(job.job_id) && ["queued", "running", "cancelling"].includes(job.status))} onClick={() => void startFormalRender()}>{submitting ? "正在建立正式輸出…" : "正式輸出（Render Job）"}</button>
            <button onClick={() => void exportProject("opencut")}>OpenCut 素材包</button>
            <button disabled={!detail.can_render} onClick={() => void exportProject("opencut-render")}>OpenCut 調色片段</button>
          </div>
        </Card>
      </div>
    </div>

    <div className="workspace-section" id="workspace-media" tabIndex={-1}>
      <WorkflowSkeleton detail={detail} />
      <div className="grid">
        <Card title="素材">
          <div className="row">
            <input type="file" multiple accept="video/*" onChange={uploadFiles} />
            <button onClick={() => void analyze(true)}>全部重跑感知</button>
            <button disabled={!detail.clips.length} onClick={() => void buildPlan()}>產生故事整理</button>
          </div>
          {detail.clips.map((clip) => <div className="item" key={clip.clip_id}>
            <div className="row">
              <b>{clip.clip_id}</b>
              <button onClick={() => void analyzeOne(clip.video_id)}>重跑感知</button>
            </div>
            {clip.filename}
            <span>{projectStatusLabel(clip.status)} · {clip.segment_count} 段 · {Math.round(clip.duration_seconds || 0)} 秒 · {clip.time_of_day || "未分類時段"}</span>
            <ClipSummary projectId={detail.project.id} clip={clip} setMessage={setMessage} refreshProject={refreshCurrentProject} />
          </div>)}
          {!detail.clips.length && <div className="inline-empty">尚無素材。先選擇多支影片匯入，再進行內容感知。</div>}
        </Card>
        <ColorConsistencyWorkspace detail={detail} setMessage={setMessage} refreshProject={refreshCurrentProject} />
      </div>
    </div>

    <div className="workspace-section" id="workspace-audio" tabIndex={-1}>
      <AudioMixingWorkspace detail={detail} bgmTracks={bgmTracks} setMessage={setMessage} refreshProject={refreshCurrentProject} />
    </div>

    <div className="workspace-section" id="workspace-script" tabIndex={-1}>
      <Card title="故事整理"><pre>{detail.script || "尚未產生故事整理。"}</pre></Card>
    </div>
  </>;

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
    try {
      const result = await api.uploadProject(detail.project.id, files);
      event.target.value = "";
      setMessage(result.ok ? `已匯入 ${result.files?.length || 0} 支素材，下一步請跑內容感知。` : result.error || "匯入失敗");
      await refreshCurrentProject({ forceFresh: true });
    } catch (error) {
      event.target.value = "";
      setMessage(`素材匯入失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  async function analyze(force: boolean) {
    setMessage(force ? "已送出全部重跑感知。" : "已送出待感知素材。");
    try {
      const result = await api.analyzeJob(detail.project.id, force);
      setMessage(result.message || "內容感知工作已開始");
      await refreshCurrentProject();
    } catch (error) {
      setMessage(`內容感知啟動失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  async function analyzeOne(videoId: number) {
    setMessage("已送出單支素材感知，請看工作狀態百分比。");
    try {
      const result = await api.analyzeVideo(detail.project.id, videoId);
      setMessage(result.message || "單支素材感知已開始");
      await refreshCurrentProject();
    } catch (error) {
      setMessage(`單支素材感知啟動失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  async function buildPlan() {
    setMessage("正在產生故事整理...");
    try {
      await api.buildPlan(detail.project.id);
      setMessage("故事整理已更新，請審核片段。");
      await refreshCurrentProject({ forceFresh: true });
    } catch (error) {
      setMessage(`故事整理失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }
}

function ClipSummary({ projectId, clip, setMessage, refreshProject }: {
  projectId: number;
  clip: { video_id: number; visual_summary: string };
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]>;
}) {
  const [text, setText] = useState(clip.visual_summary || "");
  useEffect(() => setText(clip.visual_summary || ""), [clip.visual_summary]);

  async function save() {
    setMessage("正在儲存內容感知描述...");
    try {
      const result = await api.saveClipSummary(projectId, clip.video_id, text);
      setMessage(result.ok ? "內容感知描述已儲存，專案已回到待審。" : "儲存失敗：找不到素材");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`內容感知描述儲存失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  return <div className="stack">
    <textarea value={text} onChange={(event) => setText(event.target.value)} placeholder="內容感知描述" />
    <button onClick={() => void save()}>儲存描述</button>
  </div>;
}

function Workflow({ detail }: { detail: ProjectDetail }) {
  const steps = [
    ["新增專案", true],
    ["匯入素材", detail.clips.length > 0],
    ["內容感知", detail.clips.some((clip) => clip.segment_count > 0 || clip.status === "perceived")],
    ["故事整理", detail.segments.length > 0 || Boolean(detail.script)],
    ["核准", detail.can_render],
    ["輸出", false],
  ] as const;
  return <div className="workflow">{steps.map(([label, done]) => <span key={label} className={done ? "step done" : "step"}>{label}</span>)}</div>;
}

function WorkspaceNavigation() {
  const items = [
    ["workspace-overview", "總覽"],
    ["workspace-storyboard", "分鏡"],
    ["workspace-review", "審核與輸出"],
    ["workspace-media", "素材與調色"],
    ["workspace-audio", "音訊"],
    ["workspace-script", "故事整理"],
  ] as const;
  return <nav className="workspace-nav" aria-label="專案工作區導覽">
    {items.map(([id, label]) => <a key={id} href={`#${id}`}>{label}</a>)}
  </nav>;
}

function WorkflowSkeleton({ detail }: { detail: ProjectDetail }) {
  return <Card title="OpenMontage-style 工作流骨架">
    <div className="workflow-grid">
      {detail.workflow.stages.map((stage) => <div className="workflow-card" key={stage.id}>
        <b>{stage.label}</b>
        <span className={stage.status === "done" ? "pill ok" : "pill"}>{stage.status}</span>
        <small>{stage.artifacts.join("\n")}</small>
      </div>)}
    </div>
  </Card>;
}

function WorkspaceLoading({ title, detail }: { title: string; detail: string }) {
  return <div className="workspace-state" role="status">
    <span className="workspace-spinner" aria-hidden="true" />
    <div><h2>{title}</h2><p>{detail}</p></div>
  </div>;
}

function WorkspaceEmpty({ title, detail }: { title: string; detail: string }) {
  return <div className="workspace-state empty">
    <span className="workspace-empty-icon" aria-hidden="true">＋</span>
    <div><h2>{title}</h2><p>{detail}</p></div>
  </div>;
}

function projectStatusLabel(value: string): string {
  const labels: Record<string, string> = {
    approved: "已核准",
    needs_review: "待審核",
    rejected: "已退回",
    processing: "處理中",
    perceived: "已感知",
    pending: "等待中",
    ready: "已就緒",
    failed: "失敗",
  };
  return labels[value] || value || "未知狀態";
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return <div className="card"><h3>{title}</h3>{children}</div>;
}

function Status({ value }: { value: string }) {
  return <span className={value === "approved" ? "pill ok" : "pill"}>{projectStatusLabel(value)}</span>;
}
