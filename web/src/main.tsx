import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import type { ChangeEvent, ReactNode } from "react";
import { api, BgmTrack, Job, Project, ProjectDetail, Segment } from "./api";
import "./styles.css";

function App() {
  if (window.location.pathname === "/bgm") return <BgmPage />;
  const [projects, setProjects] = useState<Project[]>([]);
  const [currentId, setCurrentId] = useState<number>(0);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [bgmTracks, setBgmTracks] = useState<BgmTrack[]>([]);
  const [notes, setNotes] = useState("");
  const [message, setMessage] = useState("");
  const [newProjectName, setNewProjectName] = useState("");

  useEffect(() => {
    loadProjects();
    api.bgm().then(setBgmTracks);
  }, []);

  function loadProjects() {
    return api.projects().then((rows) => {
      setProjects(rows);
      setCurrentId((id) => id || rows[0]?.id || 0);
    });
  }

  useEffect(() => {
    if (!currentId) return;
    let alive = true;
    const load = () => Promise.all([api.project(currentId), api.jobs(currentId)]).then(([project, jobs]) => {
      if (!alive) return;
      setDetail(project);
      setJobs(jobs);
    });
    load();
    const timer = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(timer);
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

  async function refreshProject() {
    if (!currentId) return;
    const [project, jobs] = await Promise.all([api.project(currentId), api.jobs(currentId)]);
    setDetail(project);
    setJobs(jobs);
  }

  return (
    <main>
      <aside>
        <h1>video-vault-ai</h1>
        <a className="nav" href="/bgm">BGM 資料庫</a>
        <a className="nav" href="/classic-bgm">舊版 BGM 上傳</a>
        <a className="nav" href="/classic">舊版工作台</a>
        <div className="new-project">
          <input value={newProjectName} onChange={(e) => setNewProjectName(e.target.value)} placeholder="新專案名稱" />
          <button onClick={createProject}>新增專案</button>
        </div>
        <h2>專案</h2>
        {projects.map((p) => (
          <button key={p.id} className={p.id === currentId ? "project active" : "project"} onClick={() => setCurrentId(p.id)}>
            <b>{p.name}</b>
            <span>#{p.id} | {p.status} | {p.video_count ?? 0} clips</span>
          </button>
        ))}
      </aside>
      <section>
        {message && <div className="notice">{message}</div>}
        {!detail ? <div className="card">尚未選擇專案</div> : <ProjectView detail={detail} jobs={jobs} bgmTracks={bgmTracks} notes={notes} setNotes={setNotes} setMessage={setMessage} refreshProject={refreshProject} review={review} revise={revise} />}
      </section>
    </main>
  );
}

function BgmPage() {
  const [tracks, setTracks] = useState<BgmTrack[]>([]);
  useEffect(() => {
    api.bgm().then(setTracks);
  }, []);
  return (
    <main>
      <aside>
        <h1>BGM 資料庫</h1>
        <a className="nav" href="/">專案工作台</a>
        <a className="nav" href="/classic-bgm">上傳 BGM</a>
      </aside>
      <section>
        <div className="hero"><div><h2>本地 BGM 總覽</h2><p>{tracks.length} 首可用音樂</p></div></div>
        <div className="grid">
          {tracks.map((track) => (
            <Card key={track.id} title={track.title}>
              <p>{track.artist || "未知作者"} | {track.license_name || "未填授權"} | {track.mood || "未分類"}</p>
              {track.source_url && <p><a href={track.source_url} target="_blank">來源</a></p>}
              <pre>{track.attribution_text || "尚未填寫 YouTube 署名文字。"}</pre>
            </Card>
          ))}
          {!tracks.length && <Card title="尚無 BGM"><p>請先到舊版 BGM 上傳頁登錄本地音樂。</p></Card>}
        </div>
      </section>
    </main>
  );
}

function ProjectView({ detail, jobs, bgmTracks, notes, setNotes, setMessage, refreshProject, review, revise }: {
  detail: ProjectDetail;
  jobs: Job[];
  bgmTracks: BgmTrack[];
  notes: string;
  setNotes: (value: string) => void;
  setMessage: (value: string) => void;
  refreshProject: () => Promise<void>;
  review: (action: "approve" | "reject") => void;
  revise: () => void;
}) {
  const [selectedBgm, setSelectedBgm] = useState("");
  const [colorMode, setColorMode] = useState("dji_lut");
  return (
    <>
      <div className="hero">
        <div>
          <h2>{detail.project.name}</h2>
          <p>{detail.folder}</p>
        </div>
        <Status value={detail.project.status} />
      </div>
      <Jobs jobs={jobs} projectId={detail.project.id} setMessage={setMessage} refreshProject={refreshProject} />
      <Workflow detail={detail} />
      <div className="grid">
        <Card title="審核">
          <p>Gate：{detail.can_render ? "可正式輸出" : detail.render_gate_reason}</p>
          <textarea value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="審核備註" />
          <div className="row">
            <button className="good" onClick={() => review("approve")}>核准專案</button>
            <button className="danger" onClick={() => review("reject")}>退回修改</button>
            <button onClick={revise}>依備註重建故事</button>
          </div>
        </Card>
        <Card title="輸出">
          <button onClick={() => exportProject("hyperframes")}>產生初剪專案</button>
          <button disabled={!detail.can_render} onClick={() => exportProject("hyperframes-render")}>快速輸出 MP4</button>
          <button onClick={() => exportProject("opencut")}>OpenCut 素材包</button>
          <button disabled={!detail.can_render} onClick={() => exportProject("opencut-render")}>OpenCut 調色片段</button>
        </Card>
      </div>
      <WorkflowSkeleton detail={detail} />
      <div className="grid">
        <Card title="素材">
          <div className="row">
            <input type="file" multiple accept="video/*" onChange={uploadFiles} />
            <button onClick={() => analyze(true)}>全部重跑感知</button>
            <button onClick={buildPlan}>產生故事整理</button>
          </div>
          {detail.clips.map((c) => (
            <div className="item" key={c.clip_id}>
              <div className="row">
                <b>{c.clip_id}</b>
                <button onClick={() => analyzeOne(c.video_id)}>重跑感知</button>
              </div>
              {c.filename}
              <span>{c.status} | {c.segment_count} 段 | {Math.round(c.duration_seconds || 0)}s | {c.time_of_day}</span>
              <ClipSummary projectId={detail.project.id} clip={c} setMessage={setMessage} refreshProject={refreshProject} />
            </div>
          ))}
        </Card>
        <Card title="調色預覽">
          <div className="row">
            <select value={colorMode} onChange={(e) => setColorMode(e.target.value)}>
              <option value="dji_lut">DJI LUT</option>
              <option value="safe_restore">保守修正</option>
              <option value="warm_food">咖啡/食物暖色</option>
              <option value="none">不調色</option>
            </select>
            <button onClick={colorPreview}>產生調色預覽</button>
          </div>
          <p className="muted">會依內容感知選基準畫面，並在工作狀態顯示進度。</p>
        </Card>
      </div>
      <div className="grid">
        <Card title="BGM">
          <div className="row">
            <select value={selectedBgm} onChange={(e) => setSelectedBgm(e.target.value)}>
              <option value="">選擇 BGM</option>
              {bgmTracks.map((track) => <option key={track.id} value={track.id}>{track.title}</option>)}
            </select>
            <button disabled={!selectedBgm} onClick={assignBgm}>加入本專案</button>
          </div>
          {detail.bgm.length ? detail.bgm.map((b) => <div className="item" key={b.id}>{b.title}<span>{b.attribution_text || b.artist}</span></div>) : "未指定 BGM"}
          {detail.plan.bgm_recommendations?.length ? (
            <div className="recommendations">
              <b>依內容推薦</b>
              {detail.plan.bgm_recommendations.map((item) => (
                <div className="item" key={`${item.group}-${item.track.id}`}>
                  {item.group} → {item.track.title}
                  <span>{item.activity} | {item.mood.join(", ")}</span>
                </div>
              ))}
            </div>
          ) : null}
        </Card>
      </div>
      <SegmentTable detail={detail} />
      <Card title="故事整理">
        <pre>{detail.script || "尚未產生故事整理。"}</pre>
      </Card>
    </>
  );

  async function exportProject(kind: "hyperframes" | "hyperframes-render" | "opencut" | "opencut-render") {
    setMessage("工作已送出，請看工作狀態百分比。");
    const result = kind.startsWith("hyperframes")
      ? await api.hyperframesJob(detail.project.id, kind === "hyperframes-render")
      : await api.opencutJob(detail.project.id, kind === "opencut-render");
    setMessage(result.message || (result.ok ? "工作已開始" : "工作已在執行中"));
    await refreshProject();
  }

  async function uploadFiles(event: ChangeEvent<HTMLInputElement>) {
    const files = event.target.files;
    if (!files?.length) return;
    setMessage("正在匯入素材...");
    const result = await api.uploadProject(detail.project.id, files);
    event.target.value = "";
    setMessage(result.ok ? `已匯入 ${result.files?.length || 0} 支素材，下一步請跑內容感知。` : result.error || "匯入失敗");
    await refreshProject();
  }

  async function analyze(force: boolean) {
    setMessage(force ? "已送出全部重跑感知。" : "已送出待感知素材。");
    const result = await api.analyzeJob(detail.project.id, force);
    setMessage(result.message || "內容感知工作已開始");
    await refreshProject();
  }

  async function analyzeOne(videoId: number) {
    setMessage("已送出單支素材感知，請看工作狀態百分比。");
    const result = await api.analyzeVideo(detail.project.id, videoId);
    setMessage(result.message || "單支素材感知已開始");
    await refreshProject();
  }

  async function colorPreview() {
    setMessage("已送出調色預覽，請看工作狀態百分比。");
    const result = await api.colorPreview(detail.project.id, colorMode);
    setMessage(result.message || "調色預覽已開始");
    await refreshProject();
  }

  async function buildPlan() {
    setMessage("正在產生故事整理...");
    await api.buildPlan(detail.project.id);
    setMessage("故事整理已更新，請審核片段。");
    await refreshProject();
  }

  async function assignBgm() {
    const bgmId = Number(selectedBgm);
    if (!bgmId) return;
    setMessage("正在加入 BGM...");
    await api.assignBgm(detail.project.id, bgmId);
    setSelectedBgm("");
    setMessage("BGM 已加入本專案，專案已回到待審。");
    await refreshProject();
  }
}

function ClipSummary({ projectId, clip, setMessage, refreshProject }: { projectId: number; clip: { video_id: number; visual_summary: string }; setMessage: (value: string) => void; refreshProject: () => Promise<void> }) {
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

function Workflow({ detail }: { detail: ProjectDetail }) {
  const steps = [
    ["新增專案", true],
    ["匯入素材", detail.clips.length > 0],
    ["內容感知", detail.clips.some((clip) => clip.segment_count > 0 || clip.status === "perceived")],
    ["故事整理", detail.segments.length > 0 || Boolean(detail.script)],
    ["核准", detail.can_render],
    ["輸出", false]
  ] as const;
  return <div className="workflow">{steps.map(([label, done]) => <span key={label} className={done ? "step done" : "step"}>{label}</span>)}</div>;
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

function Jobs({ jobs, projectId, setMessage, refreshProject }: { jobs: Job[]; projectId: number; setMessage: (value: string) => void; refreshProject: () => Promise<void> }) {
  if (!jobs.length) return null;
  const running = jobs.some((job) => job.status === "queued" || job.status === "running");
  async function stop() {
    const result = await api.stopJobs(projectId);
    setMessage(result.message || "已停止目前背景工作");
    await refreshProject();
  }
  return <Card title="工作狀態">{running && <button className="danger" onClick={stop}>停止目前工作</button>}{jobs.map((job, i) => <div className="item" key={i}><b>{job.kind} | {job.status} | {job.percent}%</b><span>{job.message}</span><progress value={job.percent} max={100} /></div>)}</Card>;
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

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
