import { useEffect, useRef, useState, type SyntheticEvent } from "react";
import { formatTimecode } from "./TimecodeInput";

type Props = { start: number; end: number; sourceUrl?: string; onChange?: (range: { start: number; end: number }) => void };

export function SegmentPreviewPlayer({ start, end, sourceUrl, onChange }: Props) {
  const ref = useRef<HTMLVideoElement>(null);
  const [range, setRange] = useState({ start, end });
  const [loop, setLoop] = useState(true);
  const [current, setCurrent] = useState(start);

  useEffect(() => setRange({ start, end }), [start, end]);
  useEffect(() => { if (ref.current) ref.current.currentTime = range.start; setCurrent(range.start); }, [range.start]);
  const updateRange = (next: { start: number; end: number }) => { const safe = { start: Math.max(0, next.start), end: Math.max(next.start, next.end) }; setRange(safe); onChange?.(safe); };
  const nudge = (amount: number) => { const video = ref.current; if (!video) return; video.currentTime = Math.max(range.start, Math.min(range.end, video.currentTime + amount)); };
  const onTimeUpdate = (event: SyntheticEvent<HTMLVideoElement>) => { const video = event.currentTarget; setCurrent(video.currentTime); if (video.currentTime >= range.end) { if (loop) { video.currentTime = range.start; void video.play(); } else video.pause(); } };

  return <div className="preview-player">
    <video ref={ref} src={sourceUrl} controls={Boolean(sourceUrl)} onTimeUpdate={onTimeUpdate} onLoadedMetadata={() => { if (ref.current) ref.current.currentTime = range.start; }} />
    {!sourceUrl && <div className="preview-empty">尚未接入素材 URL，可先編輯剪點</div>}
    <div className="player-readout"><b>{formatTimecode(current)}</b><span>片段範圍 {formatTimecode(range.start)} ~ {formatTimecode(range.end)}</span></div>
    <div className="player-controls"><button onClick={() => nudge(-1)}>−1 秒</button><button onClick={() => nudge(-.1)}>−0.1 秒</button><button onClick={() => nudge(.1)}>＋0.1 秒</button><button onClick={() => nudge(1)}>＋1 秒</button><button onClick={() => updateRange({ ...range, start: current })}>設為 In</button><button onClick={() => updateRange({ ...range, end: current })}>設為 Out</button><label className="toggle"><input type="checkbox" checked={loop} onChange={(e) => setLoop(e.target.checked)} /> 循環播放</label></div>
  </div>;
}
