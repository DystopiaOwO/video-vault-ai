import type { Segment } from "../../api";
import { TimecodeInput, formatTimecode } from "./TimecodeInput";

type Props = { segments: Segment[]; onChange: (segments: Segment[]) => void; onSave: () => void; onPreview?: (segment: Segment) => void };

export function SegmentEditor({ segments, onChange, onSave, onPreview }: Props) {
  const update = (index: number, patch: Partial<Segment>) => onChange(segments.map((item, i) => i === index ? { ...item, ...patch } : item));
  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= segments.length) return;
    const next = [...segments];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };

  return <section className="panel segment-panel">
    <div className="section-head"><div><h2>片段審核</h2><p className="muted">調整剪點、順序與用途後，儲存即可更新審核內容。</p></div><button onClick={onSave}>儲存片段審核</button></div>
    <div className="table"><div className="thead"><span>使用</span><span>順序</span><span>片段 / 來源</span><span>時間範圍</span><span>速度</span><span>音訊</span><span>場景 / 用途</span><span>時間軸長度</span><span>備註</span><span>預覽</span></div>
      {segments.map((item, index) => { const duration = Math.max(0, item.end_seconds - item.start_seconds) / Math.max(.25, item.speed); return <div className="trow" key={item.segment_id}>
        <span className="include-cell"><input type="checkbox" checked={item.include} onChange={(e) => update(index, { include: e.target.checked })} /><span>{item.include ? "保留" : "不用"}</span></span>
        <span className="order-cell"><button title="上移" disabled={index === 0} onClick={() => move(index, -1)}>↑</button><button title="下移" disabled={index === segments.length - 1} onClick={() => move(index, 1)}>↓</button><b>{index + 1}</b></span>
        <span className="clip-cell"><b>{item.clip_id}</b><span>{item.title}</span><small>{item.source_file ?? "未指定來源"}</small></span>
        <span className="time-range"><TimecodeInput label="開始" value={item.start_seconds} onChange={(value) => update(index, { start_seconds: value })} /><b>~</b><TimecodeInput label="結束" value={item.end_seconds} onChange={(value) => update(index, { end_seconds: value })} /></span>
        <label className="compact-field"><span className="sr-only">速度</span><input type="number" min="0.25" max="4" step="0.05" value={item.speed} onChange={(e) => update(index, { speed: Math.min(4, Math.max(.25, Number(e.target.value) || 1)) })} /><small>x</small></label>
        <select aria-label="音訊角色" value={item.audio_role} onChange={(e) => update(index, { audio_role: e.target.value })}><option value="keep_original">保留原音</option><option value="lower_original">降低原音</option><option value="mute">靜音</option><option value="dialogue">對白</option></select>
        <span className="scene-cell"><b>{item.scene_role}</b><span>{item.suggested_use}</span></span>
        <span className="duration-cell">{formatTimecode(duration)}</span>
        <input aria-label={`${item.clip_id} 備註`} value={item.user_notes} onChange={(e) => update(index, { user_notes: e.target.value })} />
        <button className="icon-action" title="預覽片段" onClick={() => onPreview?.(item)}>▶</button>
      </div>; })}
    </div>
    <p className="review-hint">修改後需要重新核准</p>
  </section>;
}
