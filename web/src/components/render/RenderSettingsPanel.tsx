import type { RenderSettings, Segment } from "../../api";

type Props = { settings: RenderSettings; segments: Segment[]; onChange: (value: RenderSettings) => void; disabled?: boolean };
export function RenderSettingsPanel({ settings, segments, onChange, disabled }: Props) {
  const set = (patch: Partial<RenderSettings>) => onChange({ ...settings, ...patch });
  const duration = segments.filter((item) => item.include).reduce((sum, item) => sum + Math.max(0, item.end_seconds - item.start_seconds) / Math.max(.25, item.speed), 0);
  const disk = Math.max(1, Math.ceil(duration * (settings.finalProfile.includes("2160") ? 18 : 8)));
  return <div className="settings-panel">
    <label>預覽 Profile<select disabled={disabled} value={settings.previewProfile} onChange={(e) => set({ previewProfile: e.target.value })}><option>preview_1080p30</option><option>preview_1080p60</option></select></label>
    <label>正式 Profile<select disabled={disabled} value={settings.finalProfile} onChange={(e) => set({ finalProfile: e.target.value })}><option>final_1080p30</option><option>final_1080p60</option><option>final_2160p30</option><option>final_2160p60</option></select></label>
    <label>調色模式<select disabled={disabled} value={settings.colorMode} onChange={(e) => set({ colorMode: e.target.value })}><option value="none">不調色</option><option value="dji_lut">DJI LUT</option><option value="safe_restore">保守修正</option><option value="warm_food">暖色食物</option></select></label>
    <label>編碼器<select disabled={disabled} value={settings.encoder} onChange={(e) => set({ encoder: e.target.value })}><option value="auto">自動 / GPU 優先</option><option value="h264_nvenc">NVIDIA NVENC</option><option value="h264_amf">AMD AMF</option><option value="libx264">CPU fallback</option></select></label>
    <label>BGM 音量<input disabled={disabled} type="number" min="0" max="1" step=".05" value={settings.bgmVolume} onChange={(e) => set({ bgmVolume: Number(e.target.value) })} /></label>
    <label>音訊交疊（秒）<input disabled={disabled} type="number" min="0" max="2" step=".01" value={settings.audioCrossfade} onChange={(e) => set({ audioCrossfade: Number(e.target.value) })} /></label>
    <label>BGM 淡入（秒）<input disabled={disabled} type="number" min="0" max="10" step=".1" value={settings.bgmFadeIn} onChange={(e) => set({ bgmFadeIn: Number(e.target.value) })} /></label>
    <label>BGM 淡出（秒）<input disabled={disabled} type="number" min="0" max="10" step=".1" value={settings.bgmFadeOut} onChange={(e) => set({ bgmFadeOut: Number(e.target.value) })} /></label>
    <label className="check"><input disabled={disabled} type="checkbox" checked={settings.overlayEnabled} onChange={(e) => set({ overlayEnabled: e.target.checked })} /> 套用字卡與 Overlay</label>
    <div className="estimate"><span>估算時間軸</span><b>{duration.toFixed(3)} 秒</b><span>估算輸出大小</span><b>約 {disk} MB</b></div>
  </div>;
}
