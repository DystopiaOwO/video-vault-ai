import { useMemo, useState } from "react";
import type { Clip, SamplingOverride } from "../../api";

type Props = {
  clip: Pick<Clip, "filename" | "duration_seconds" | "sampling">;
  disabled?: boolean;
  onAnalyze: (sampling: SamplingOverride) => void | Promise<void>;
};

export function PerceptionSamplingControls({ clip, disabled, onAnalyze }: Props) {
  const currentPolicy = clip.sampling?.current?.policy || clip.sampling?.default_policy;
  const [mode, setMode] = useState<"fixed" | "adaptive">(currentPolicy?.mode || "adaptive");
  const [preset, setPreset] = useState<"balanced" | "dense">(currentPolicy?.preset || "balanced");
  const [interval, setInterval] = useState(Number(currentPolicy?.baseline_interval_seconds || 5));
  const [maximum, setMaximum] = useState(Number(currentPolicy?.max_frames_per_clip || 180));
  const current = clip.sampling?.current;
  const estimate = useMemo(() => {
    const duration = Math.max(0, Number(clip.duration_seconds || 0));
    const baseline = Math.max(1, Math.ceil(duration / Math.max(0.5, interval)));
    const boundary = mode === "adaptive" && duration > 0 ? 2 : 0;
    return Math.min(maximum, baseline + boundary);
  }, [clip.duration_seconds, interval, maximum, mode]);
  const reasons = current?.sample_reason_counts || {};

  return <section className="perception-sampling" aria-label={`${clip.filename} 感知取樣設定`}>
    <div className="clip-summary-heading">
      <strong>感知取樣</strong>
      <span>預估 {estimate} 張／次模型呼叫上限 {maximum}</span>
    </div>
    <div className="row">
      <label>
        模式
        <select value={mode} disabled={disabled} onChange={(event) => setMode(event.target.value as "fixed" | "adaptive")}>
          <option value="adaptive">自適應</option>
          <option value="fixed">固定間隔</option>
        </select>
      </label>
      {mode === "adaptive" && <label>
        密度
        <select value={preset} disabled={disabled} onChange={(event) => setPreset(event.target.value as "balanced" | "dense")}>
          <option value="balanced">平衡</option>
          <option value="dense">較密集</option>
        </select>
      </label>}
      <label>
        基準秒數
        <input type="number" min="0.5" max="60" step="0.5" value={interval} disabled={disabled} onChange={(event) => setInterval(Number(event.target.value || 5))} />
      </label>
      <label>
        每支上限
        <input type="number" min="1" max="2000" step="1" value={maximum} disabled={disabled} onChange={(event) => setMaximum(Number(event.target.value || 180))} />
      </label>
      <button
        type="button"
        disabled={disabled}
        onClick={() => void onAnalyze({
          mode,
          preset,
          baseline_interval_seconds: interval,
          max_frames_per_clip: maximum,
        })}
      >
        依此設定重跑
      </button>
    </div>
    {current?.policy && <small>
      上次：{current.policy.mode === "adaptive" ? "自適應" : "固定"}／
      實際 {current.samples?.length || 0} 張／模型 {current.actual_vision_calls || 0} 次／
      快取 {current.cache_hits || 0} 次／
      baseline {reasons.baseline || 0}、scene {reasons.scene || 0}、motion {reasons.motion || 0}、boundary {reasons.boundary || 0}
      {current.visual_dedupe?.removed ? `／視覺去重 ${current.visual_dedupe.removed}` : ""}
    </small>}
  </section>;
}
