import { StrictMode, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { JoinPreviewPanel } from "./components/render/JoinPreviewPanel";
import { RenderActions } from "./components/render/RenderActions";
import { RenderJobPanel } from "./components/render/RenderJobPanel";
import { RenderOutputsPanel } from "./components/render/RenderOutputsPanel";
import { RenderSettingsPanel } from "./components/render/RenderSettingsPanel";
import { SegmentEditor } from "./components/render/SegmentEditor";
import { SegmentPreviewPlayer } from "./components/render/SegmentPreviewPlayer";
import { mockJobs, mockProject, mockSegments, type RenderSettings, type Segment } from "./api";
import "./styles.css";

function App() {
  const [segments, setSegments] = useState<Segment[]>(mockSegments);
  const [selected, setSelected] = useState(mockSegments[0]);
  const [jobs, setJobs] = useState(mockJobs);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("請先審核片段；正式輸出會在核准後解鎖。 ");
  const [settings, setSettings] = useState<RenderSettings>({ previewProfile: "preview_1080p30", finalProfile: "final_1080p30", colorMode: "dji_lut", audioCrossfade: .08, bgmVolume: .35, bgmFadeIn: 1, bgmFadeOut: 2, overlayEnabled: true, encoder: "auto" });
  const selectedSegment = segments.find((item) => item.segment_id === selected.segment_id) ?? segments[0];
  const includedCount = useMemo(() => segments.filter((item) => item.include).length, [segments]);
  const run = (action: string) => { setBusy(true); setMessage(`${action} 已加入工作佇列，等待後端 API 接入。`); window.setTimeout(() => setBusy(false), 500); };
  const updateSegments = (next: Segment[]) => { setSegments(next); const nextSelected = next.find((item) => item.segment_id === selected.segment_id); if (nextSelected) setSelected(nextSelected); setMessage("片段已修改，修改後需要重新核准。"); };
  return <main>
    <aside><h1>video-vault-ai</h1><p className="muted">Render Pipeline v2</p><div className="project active"><b>{mockProject.name}</b><span>{mockProject.status} · {includedCount} clips</span></div><div className="sidebar-meta"><span>目前編碼器</span><b>{settings.encoder === "auto" ? "GPU 優先" : settings.encoder}</b></div></aside>
    <section className="workspace"><header className="hero"><div><p className="eyebrow">PROJECT / RENDER REVIEW</p><h2>{mockProject.name}</h2><p>先確認片段與設定，再進行正式輸出</p></div><span className="pill">{mockProject.status}</span></header>
      <div className="notice" role="status">{message}</div>
      <div className="grid"><section className="panel"><div className="section-head"><h2>渲染設定</h2><span className="muted">變更後需重新核准</span></div><RenderSettingsPanel settings={settings} segments={segments} onChange={(next) => { setSettings(next); setMessage("渲染設定已修改，修改後需要重新核准。"); }} /></section><section className="panel"><div className="section-head"><h2>渲染操作</h2><span className="muted">{includedCount} 個片段已納入</span></div><RenderActions canRender={mockProject.canRender} gateReason={mockProject.gateReason} busy={busy} onAction={run} /></section></div>
      <section className="panel"><div className="section-head"><h2>工作狀態</h2><span className="muted">即時進度與可取消工作</span></div><RenderJobPanel jobs={jobs} onCancel={(job) => { setJobs((current) => current.map((item) => item.job_id === job?.job_id ? { ...item, status: "cancelled", message: "已取消" } : item)); setMessage("已送出取消要求。"); }} /></section>
      <SegmentEditor segments={segments} onChange={updateSegments} onSave={() => setMessage("片段審核已儲存，修改後需要重新核准。")} onPreview={setSelected} />
      <div className="grid preview-grid"><JoinPreviewPanel segments={segments} /><section className="panel"><div className="section-head"><h2>片段預覽</h2><span className="muted">{selectedSegment?.clip_id}</span></div>{selectedSegment && <SegmentPreviewPlayer start={selectedSegment.start_seconds} end={selectedSegment.end_seconds} sourceUrl={selectedSegment.source_url} onChange={(range) => { setSegments((current) => current.map((item) => item.segment_id === selectedSegment.segment_id ? { ...item, start_seconds: range.start, end_seconds: range.end } : item)); setMessage("預覽剪點已更新，修改後需要重新核准。"); }} />}</section></div>
      <section className="panel"><div className="section-head"><h2>輸出檔案</h2><span className="muted">通過 QC 後才會出現正式檔</span></div><RenderOutputsPanel /></section>
    </section>
  </main>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
