import { StrictMode, useState } from "react";
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
  const [message, setMessage] = useState("這是 Render Pipeline v2 UI skeleton，尚未接正式 API。");
  const [settings, setSettings] = useState<RenderSettings>({ previewProfile: "preview_1080p30", finalProfile: "final_1080p30", colorMode: "dji_lut", audioCrossfade: .08, bgmVolume: .35, overlayEnabled: true, encoder: "auto" });
  const run = (action: string) => setMessage(`${action} 已建立 UI action，等待 Wave 2 API 接入。`);
  return <main><aside><h1>video-vault-ai</h1><p className="muted">Render Pipeline v2</p><div className="project active"><b>{mockProject.name}</b><span>{mockProject.status} · 2 clips</span></div></aside><section><header className="hero"><div><h2>{mockProject.name}</h2><p>人工審核後才能正式輸出</p></div><span className="pill">{mockProject.status}</span></header><div className="notice">{message}</div><div className="grid"><section className="panel"><h2>渲染設定</h2><RenderSettingsPanel settings={settings} onChange={(next) => { setSettings(next); setMessage("設定已修改，需重新核准。"); }} /></section><section className="panel"><h2>渲染操作</h2><RenderActions canRender={mockProject.canRender} onAction={run} /></section></div><section className="panel"><div className="section-head"><h2>工作狀態</h2></div><RenderJobPanel jobs={mockJobs} onCancel={() => setMessage("取消 action 已準備，等待 Job API 接入。")} /></section><SegmentEditor segments={segments} onChange={setSegments} onSave={() => setMessage("片段審核已儲存，需重新核准。")} /><JoinPreviewPanel segments={segments} /><section className="panel"><h2>片段預覽</h2><SegmentPreviewPlayer start={segments[0].start_seconds} end={segments[0].end_seconds} /></section><section className="panel"><h2>輸出檔案</h2><RenderOutputsPanel /></section></section></main>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
