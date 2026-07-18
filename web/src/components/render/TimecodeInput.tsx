import { useEffect, useState } from "react";

type Props = { value: number; onChange: (seconds: number) => void; label: string; disabled?: boolean };

export function formatTimecode(seconds: number) {
  const totalMs = Math.floor(Math.max(0, Number.isFinite(seconds) ? seconds : 0) * 1000 + 0.5);
  const hours = Math.floor(totalMs / 3600000);
  const minutes = Math.floor((totalMs % 3600000) / 60000);
  const secs = Math.floor((totalMs % 60000) / 1000);
  const ms = totalMs % 1000;
  const base = hours ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}` : `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${base}.${String(ms).padStart(3, "0")}`;
}

function parseTimecode(text: string): number | null {
  const parts = text.trim().split(":");
  if (!parts.length || parts.length > 3 || parts.some((part) => !/^\d+(?:\.\d+)?$/.test(part))) return null;
  const numbers = parts.map(Number);
  if (parts.length === 3) return numbers[0] * 3600 + numbers[1] * 60 + numbers[2];
  if (parts.length === 2) return numbers[0] * 60 + numbers[1];
  return numbers[0];
}

export function TimecodeInput({ value, onChange, label, disabled }: Props) {
  const [draft, setDraft] = useState(formatTimecode(value));
  const [invalid, setInvalid] = useState(false);
  useEffect(() => { if (!invalid) setDraft(formatTimecode(value)); }, [value, invalid]);
  const commit = () => { const next = parseTimecode(draft); if (next === null) { setInvalid(true); return; } setInvalid(false); onChange(next); setDraft(formatTimecode(next)); };
  const nudge = (delta: number) => { const next = Math.max(0, value + delta); setInvalid(false); onChange(next); setDraft(formatTimecode(next)); };
  return <span className="timecode-control"><input aria-label={label} value={draft} disabled={disabled} aria-invalid={invalid} onChange={(e) => { setDraft(e.target.value); setInvalid(false); }} onBlur={commit} onKeyDown={(e) => e.key === "Enter" && commit()} /><button type="button" title="減少 0.1 秒" disabled={disabled} onClick={() => nudge(-.1)}>−</button><button type="button" title="增加 0.1 秒" disabled={disabled} onClick={() => nudge(.1)}>＋</button><button type="button" title="減少 1 秒" disabled={disabled} onClick={() => nudge(-1)}>−1</button><button type="button" title="增加 1 秒" disabled={disabled} onClick={() => nudge(1)}>＋1</button>{invalid && <small className="error">時間格式錯誤</small>}</span>;
}
