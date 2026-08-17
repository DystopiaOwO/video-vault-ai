import { useEffect, useMemo, useState } from "react";
import { api, type CreativeBrief, type CreativeBriefFraming, type ProjectDetail } from "../../api";
import { formatApiError } from "../../api";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import type { ProjectMutationControls } from "../../projectMutation";
import "./creative-brief.css";

type Props = {
  detail: ProjectDetail;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<unknown>;
  mutationControls: ProjectMutationControls;
};

const OUTPUTS: Record<string, { orientation: string; aspect_ratio: string; width: number; height: number; render_profile_id: string }> = {
  landscape: { orientation: "landscape", aspect_ratio: "16:9", width: 1920, height: 1080, render_profile_id: "final_1080p" },
  portrait: { orientation: "portrait", aspect_ratio: "9:16", width: 1080, height: 1920, render_profile_id: "final_1080p_portrait" },
};

const STRATEGIES = [
  ["crop_reframe", "裁切／重新構圖"],
  ["background_treatment", "背景處理（VID-27）"],
  ["preserve_full_frame", "保留完整畫面"],
] as const;

function outputFor(orientation: string) {
  return OUTPUTS[orientation] || OUTPUTS.landscape;
}

function framingValue(framing: CreativeBriefFraming | undefined, fallback = "crop_reframe") {
  return String(framing?.approved_strategy || framing?.recommended_strategy || fallback);
}

export function CreativeBriefCheckpoint({ detail, setMessage, refreshProject, mutationControls }: Props) {
  const brief = detail.creative_brief || {};
  const recommendation = brief.recommendation || {};
  const recommendedOutput = recommendation.output || outputFor("landscape");
  const approved = brief.approved || {};
  const initialOutput = approved.output || recommendedOutput;
  const [orientation, setOrientation] = useState(String(initialOutput.orientation || "landscape"));
  const [portraitInLandscape, setPortraitInLandscape] = useState(framingValue(approved.framing_intent?.portrait_source_in_landscape || recommendation.framing_intent?.portrait_source_in_landscape));
  const [landscapeInPortrait, setLandscapeInPortrait] = useState(framingValue(approved.framing_intent?.landscape_source_in_portrait || recommendation.framing_intent?.landscape_source_in_portrait));
  const [busy, setBusy] = useState("");
  const output = useMemo(() => outputFor(orientation), [orientation]);
  const counts = recommendation.source_orientation_summary || brief.source_geometry?.orientation_counts || {};
  const approvedState = brief.status === "approved";

  useEffect(() => {
    const nextApproved = brief.approved || {};
    const nextRecommendation = brief.recommendation || {};
    const nextOutput = nextApproved.output || nextRecommendation.output || outputFor("landscape");
    setOrientation(String(nextOutput.orientation || "landscape"));
    setPortraitInLandscape(framingValue(nextApproved.framing_intent?.portrait_source_in_landscape || nextRecommendation.framing_intent?.portrait_source_in_landscape));
    setLandscapeInPortrait(framingValue(nextApproved.framing_intent?.landscape_source_in_portrait || nextRecommendation.framing_intent?.landscape_source_in_portrait));
  }, [brief.brief_version, brief.status]);

  async function refreshRecommendation() {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy("recommend");
    try {
      const result = await api.recommendCreativeBrief(detail.project.id, detail.project_revision);
      if (!result.ok || !result.creative_brief) throw new Error(result.error || "素材方向分析失敗");
      await refreshProject({ forceFresh: true });
      setMessage("已重新整理素材 display geometry 與 Creative Brief 建議；尚未核准。");
    } catch (error) {
      setMessage(`Creative Brief 建議失敗：${formatApiError(error)}`);
    } finally {
      mutationControls.finishProjectMutation(mutation);
      setBusy("");
    }
  }

  async function save(approvalSource: "recommendation" | "human_override", values?: { orientation: string; portraitInLandscape: string; landscapeInPortrait: string }) {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy("save");
    try {
      const nextOrientation = values?.orientation || orientation;
      const nextOutput = outputFor(nextOrientation);
      const nextPortraitInLandscape = values?.portraitInLandscape || portraitInLandscape;
      const nextLandscapeInPortrait = values?.landscapeInPortrait || landscapeInPortrait;
      const result = await api.saveCreativeBrief(detail.project.id, {
        output: nextOutput,
        framing_intent: {
          portrait_source_in_landscape: { approved_strategy: nextPortraitInLandscape },
          landscape_source_in_portrait: { approved_strategy: nextLandscapeInPortrait },
        },
      }, approvalSource, detail.project_revision);
      if (!result.ok || !result.creative_brief) throw new Error(result.error || "Creative Brief 儲存失敗");
      await refreshProject({ forceFresh: true });
      setMessage(approvalSource === "recommendation" ? "已採用 AI 建議並核准 Creative Brief。" : "已儲存手動覆寫並核准 Creative Brief。");
    } catch (error) {
      setMessage(`Creative Brief 儲存失敗：${formatApiError(error)}`);
    } finally {
      mutationControls.finishProjectMutation(mutation);
      setBusy("");
    }
  }

  return <section className="creative-brief card" aria-label="Creative Brief checkpoint">
    <div className="creative-brief-heading">
      <div><span className="eyebrow">CREATIVE BRIEF CHECKPOINT</span><h3>先確認最終影片方向</h3><p>這是 Story generation 前的 project-level visual contract；實際 smart crop、背景處理與預覽屬於 VID-27。</p></div>
      <span className={approvedState ? "brief-status approved" : "brief-status"}>{approvedState ? "已核准" : "待人工確認"}</span>
    </div>
    <div className="creative-brief-summary" aria-label="素材方向摘要">
      <span>直向素材：{counts.portrait ?? 0}</span><span>橫向素材：{counts.landscape ?? 0}</span><span>方形／未知：{(counts.square ?? 0) + (counts.unknown ?? 0)}</span>
      <button type="button" disabled={Boolean(busy)} onClick={() => void refreshRecommendation()}>{busy === "recommend" ? "分析中…" : "重新分析素材方向"}</button>
    </div>
    <div className="creative-brief-recommendation">
      <strong>AI 建議：{String(recommendedOutput.orientation === "portrait" ? "直向 9:16" : "橫向 16:9")}</strong>
      <span>{recommendation.reason || "尚無建議理由"}</span>
    </div>
    <div className="creative-brief-controls">
      <fieldset><legend>最終影片方向</legend>
        <label><input type="radio" name={`brief-orientation-${detail.project.id}`} checked={orientation === "landscape"} disabled={Boolean(busy)} onChange={() => setOrientation("landscape")} />橫向 16:9（1920×1080）</label>
        <label><input type="radio" name={`brief-orientation-${detail.project.id}`} checked={orientation === "portrait"} disabled={Boolean(busy)} onChange={() => setOrientation("portrait")} />直向 9:16（1080×1920）</label>
      </fieldset>
      <div className="creative-brief-output"><span>目前選擇</span><strong>{output.width}×{output.height}</strong><code>{output.render_profile_id}</code></div>
      <label>橫向輸出 + 直向素材<select value={portraitInLandscape} disabled={Boolean(busy)} onChange={(event) => setPortraitInLandscape(event.target.value)}>{STRATEGIES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>直向輸出 + 橫向素材<select value={landscapeInPortrait} disabled={Boolean(busy)} onChange={(event) => setLandscapeInPortrait(event.target.value)}>{STRATEGIES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    </div>
    <div className="creative-brief-actions">
      <button type="button" className="primary" disabled={Boolean(busy)} onClick={() => { const nextOrientation = String(recommendedOutput.orientation || "landscape"); const nextPortrait = framingValue(recommendation.framing_intent?.portrait_source_in_landscape); const nextLandscape = framingValue(recommendation.framing_intent?.landscape_source_in_portrait); setOrientation(nextOrientation); setPortraitInLandscape(nextPortrait); setLandscapeInPortrait(nextLandscape); void save("recommendation", { orientation: nextOrientation, portraitInLandscape: nextPortrait, landscapeInPortrait: nextLandscape }); }}>{busy === "save" ? "儲存中…" : "採用 AI 建議並核准"}</button>
      <button type="button" disabled={Boolean(busy)} onClick={() => void save("human_override")}>儲存並核准手動覆寫</button>
    </div>
    {approvedState && <p className="creative-brief-approved" role="status">已核准值會由 Render / VID-27 讀取；visual-only 變更不會讓既有 StoryInput 無謂 stale。</p>}
  </section>;
}
