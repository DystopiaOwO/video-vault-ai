import { useEffect, useState } from "react";
import { api, type ProjectDetail, type StoryChapter, type StoryGeneration } from "../../api";
import { formatApiError } from "../../api";
import type { ProjectMutationControls } from "../../projectMutation";
import "./story-understanding.css";

type Props = {
  detail: ProjectDetail;
  setMessage: (value: string) => void;
  refreshProject: (options?: { forceFresh?: boolean }) => Promise<unknown>;
  mutationControls: ProjectMutationControls;
};

function draftFor(generation?: StoryGeneration) {
  const response = generation?.normalized_response || {};
  const review = generation?.review_state || {};
  return {
    project_summary: review.project_summary ?? response.project_summary ?? "",
    chapters: (review.chapters ?? response.chapters ?? []).map((chapter) => ({ ...chapter, segment_uuids: [...(chapter.segment_uuids || [])] })),
  };
}

export function StoryUnderstandingWorkspace({ detail, setMessage, refreshProject, mutationControls }: Props) {
  const story = detail.story;
  const [settings, setSettings] = useState(story?.settings || {});
  const [creator, setCreator] = useState<Record<string, unknown>>(story?.creator_profile || {});
  const [draft, setDraft] = useState(draftFor(story?.current_generation));
  const [locked, setLocked] = useState(Boolean(story?.current_generation?.review_state?.locked));
  const [busy, setBusy] = useState("");

  useEffect(() => {
    setSettings(story?.settings || {});
    setCreator(story?.creator_profile || {});
    setDraft(draftFor(story?.current_generation));
    setLocked(Boolean(story?.current_generation?.review_state?.locked));
  }, [story?.current_story_generation_uuid, story?.settings, story?.creator_profile]);

  const generation = story?.current_generation;
  const chapters = draft.chapters;
  async function saveSettings() {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy("settings");
    try {
      const result = await api.saveStorySettings(detail.project.id, settings, creator, detail.project_revision);
      if (!result.ok) throw new Error(result.error || "故事設定儲存失敗");
      await refreshProject({ forceFresh: true });
      setMessage("故事設定已儲存；下次生成故事時才會套用新的 Profile。 ");
    } catch (error) {
      setMessage(`故事設定失敗：${formatApiError(error)}`);
    } finally {
      mutationControls.finishProjectMutation(mutation);
      setBusy("");
    }
  }

  async function generate(force = false) {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy("generate");
    try {
      const result = await api.generateStory(detail.project.id, force, undefined, detail.project_revision);
      if (!result.ok) throw new Error(result.error || "故事生成失敗");
      await refreshProject({ forceFresh: true });
      setMessage(`故事 generation 已完成：${result.generation?.story_generation_uuid || ""}`);
    } catch (error) {
      setMessage(`故事生成失敗：${formatApiError(error)}`);
    } finally {
      mutationControls.finishProjectMutation(mutation);
      setBusy("");
    }
  }

  async function saveReview() {
    if (!generation) return;
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy("review");
    try {
      const result = await api.updateStoryReview(detail.project.id, generation.story_generation_uuid, { ...draft, locked }, detail.project_revision);
      if (!result.ok) throw new Error(result.error || "故事審核儲存失敗");
      await refreshProject({ forceFresh: true });
      setMessage("故事人工修改已儲存；尚未套用到分鏡。");
    } catch (error) {
      setMessage(`故事審核失敗：${formatApiError(error)}`);
    } finally {
      mutationControls.finishProjectMutation(mutation);
      setBusy("");
    }
  }

  async function applyToStoryboard() {
    if (!generation) return;
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy("apply");
    try {
      const result = await api.applyStory(detail.project.id, generation.story_generation_uuid, detail.project_revision);
      if (!result.ok) throw new Error(result.error || "套用分鏡失敗");
      await refreshProject({ forceFresh: true });
      setMessage(result.approval_invalidated ? "故事已套用到分鏡；輸出內容變更，請重新核准。" : "故事已套用到分鏡。");
    } catch (error) {
      setMessage(`套用分鏡失敗：${formatApiError(error)}`);
    } finally {
      mutationControls.finishProjectMutation(mutation);
      setBusy("");
    }
  }

  function patchChapter(index: number, patch: Partial<StoryChapter>) {
    setDraft((current) => ({ ...current, chapters: current.chapters.map((chapter, chapterIndex) => chapterIndex === index ? { ...chapter, ...patch } : chapter) }));
  }

  function moveChapter(index: number, direction: -1 | 1) {
    setDraft((current) => {
      const nextIndex = index + direction;
      if (nextIndex < 0 || nextIndex >= current.chapters.length) return current;
      const chapters = [...current.chapters];
      [chapters[index], chapters[nextIndex]] = [chapters[nextIndex], chapters[index]];
      return { ...current, chapters };
    });
  }

  return <section className="story-understanding card" aria-label="專案故事理解">
    <div className="story-understanding-heading">
      <div><span className="eyebrow">PROJECT STORY UNDERSTANDING</span><h3>故事理解與人工審核</h3><p>故事生成只提供建議，必須明確套用到既有分鏡後才會改變剪輯流程。</p></div>
      <div className="row">
        <button type="button" disabled={Boolean(busy)} onClick={() => void saveSettings()}>儲存設定</button>
        <button type="button" className="primary" disabled={Boolean(busy)} onClick={() => void generate()}>{busy === "generate" ? "生成中…" : "生成故事"}</button>
        {generation && <button type="button" disabled={Boolean(busy)} onClick={() => void generate(true)}>重新生成</button>}
      </div>
    </div>
    <div className="story-settings-grid">
      <label>Story Profile<select value={String(settings.profile_id || "general_diary")} disabled={Boolean(busy)} onChange={(event) => setSettings({ ...settings, profile_id: event.target.value })}>
        <option value="travel_diary">travel_diary｜旅行日記</option><option value="coffee_matcha_diary">coffee_matcha_diary｜咖啡／抹茶日記</option><option value="roasting_diary">roasting_diary｜烘豆日記</option><option value="general_diary">general_diary｜一般日記</option>
      </select></label>
      <label>文字語言<input value={String(creator.language || "zh-TW")} disabled={Boolean(busy)} onChange={(event) => setCreator({ ...creator, language: event.target.value })} /></label>
      <label>字卡密度<select value={String(creator.title_card_density || "low")} disabled={Boolean(busy)} onChange={(event) => setCreator({ ...creator, title_card_density: event.target.value })}><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label>
      <label>節奏偏好<input value={String(settings.desired_pacing || "")} disabled={Boolean(busy)} placeholder="例如：自然、慢節奏、保留等待" onChange={(event) => setSettings({ ...settings, desired_pacing: event.target.value })} /></label>
      <label className="wide">Creator wording style<input value={String(creator.wording_style || "")} disabled={Boolean(busy)} onChange={(event) => setCreator({ ...creator, wording_style: event.target.value })} /></label>
      <label className="wide">專案故事意圖<textarea value={String(settings.project_intent || "")} disabled={Boolean(busy)} placeholder="這支影片想讓觀眾感受到什麼？" onChange={(event) => setSettings({ ...settings, project_intent: event.target.value })} /></label>
      <label className="wide">行程／場景順序<textarea value={String(settings.itinerary || "")} disabled={Boolean(busy)} placeholder="例如：早上車站，下午展覽，傍晚咖啡廳" onChange={(event) => setSettings({ ...settings, itinerary: event.target.value })} /></label>
      <label className="wide">必留內容<input value={(settings.must_keep || []).join(", ")} disabled={Boolean(busy)} onChange={(event) => setSettings({ ...settings, must_keep: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) })} /></label>
      <label className="wide">排除指引<input value={(settings.exclude_guidance || []).join(", ")} disabled={Boolean(busy)} onChange={(event) => setSettings({ ...settings, exclude_guidance: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) })} /></label>
    </div>
    {!generation && <p className="inline-empty">尚未生成故事。請先儲存設定，再按「生成故事」。</p>}
    {generation && <>
      <div className="story-generation-meta"><b>{generation.story_generation_uuid}</b><span>{generation.provider} / {generation.model}</span><span>input {generation.input_hash?.slice(0, 12)}</span><span>狀態：{generation.status}</span></div>
      <label className="wide">專案摘要<textarea value={draft.project_summary} disabled={Boolean(busy)} onChange={(event) => setDraft({ ...draft, project_summary: event.target.value })} /></label>
      <div className="story-chapters">
        {chapters.map((chapter, index) => <article className="story-chapter" key={chapter.chapter_id || index}>
          <div className="story-chapter-head"><strong>章節 {index + 1}</strong><span>信心 {Math.round((chapter.confidence || 0) * 100)}% · {chapter.locked ? "已鎖定" : "未鎖定"}</span></div>
          <label>標題<input value={chapter.title || ""} disabled={Boolean(busy)} onChange={(event) => patchChapter(index, { title: event.target.value })} /></label>
          <label>目的<textarea value={chapter.purpose || ""} disabled={Boolean(busy)} onChange={(event) => patchChapter(index, { purpose: event.target.value })} /></label>
          <label>片段順序<textarea value={(chapter.segment_uuids || []).join(", ")} onChange={(event) => patchChapter(index, { segment_uuids: event.target.value.split(",").map((id) => id.trim()).filter(Boolean) })} /></label>
          <label>節奏<input value={chapter.pacing_intent || ""} disabled={Boolean(busy)} onChange={(event) => patchChapter(index, { pacing_intent: event.target.value })} /></label>
          <label>自然音<input value={chapter.natural_audio_intent || ""} disabled={Boolean(busy)} onChange={(event) => patchChapter(index, { natural_audio_intent: event.target.value })} /></label>
          <div className="row"><button type="button" disabled={Boolean(busy) || index === 0} onClick={() => moveChapter(index, -1)}>章節上移</button><button type="button" disabled={Boolean(busy) || index === chapters.length - 1} onClick={() => moveChapter(index, 1)}>章節下移</button><button type="button" disabled={Boolean(busy)} onClick={() => patchChapter(index, { locked: !chapter.locked })}>{chapter.locked ? "解除本章鎖定" : "鎖定本章"}</button></div>
        </article>)}
      </div>
      <div className="row story-actions">
        <button type="button" disabled={Boolean(busy)} onClick={() => void saveReview()}>{busy === "review" ? "儲存中…" : "儲存故事修改"}</button>
        <button type="button" disabled={Boolean(busy)} onClick={() => setLocked((value) => !value)}>{locked ? "解除鎖定故事" : "鎖定故事修改"}</button>
        <button type="button" className="primary" disabled={Boolean(busy)} onClick={() => void applyToStoryboard()}>{busy === "apply" ? "套用中…" : "套用到既有分鏡"}</button>
      </div>
      <p className="muted">套用前不會修改 storyboard.json、approval 或正式輸出；鎖定的既有分鏡片段會在套用時保留。</p>
    </>}
  </section>;
}
