import { useEffect, useState } from "react";
import { api, ApiError, type ProjectDetail, type StoryChapter, type StoryGeneration } from "../../api";
import { formatApiError } from "../../api";
import { refreshFailureMessage, type ProjectMutationControls } from "../../projectMutation";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import "./story-understanding.css";

type Props = {
  detail: ProjectDetail;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<unknown>;
  mutationControls: ProjectMutationControls;
};

type SegmentWithUuid = { segment_uuid?: string };
type ApplyUiState =
  | "idle"
  | "apply_succeeded"
  | "post_apply_refresh_pending"
  | "post_apply_refresh_failed"
  | "generation_archived_after_apply"
  | "apply_failed"
  | "stale_before_apply";

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
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [creatorDirty, setCreatorDirty] = useState(false);
  const [draftDirty, setDraftDirty] = useState(false);
  const [lockedDirty, setLockedDirty] = useState(false);
  const [applyUiState, setApplyUiState] = useState<ApplyUiState>("idle");
  const [appliedGenerationUuid, setAppliedGenerationUuid] = useState("");

  useEffect(() => {
    if (!settingsDirty) setSettings(story?.settings || {});
    if (!creatorDirty) setCreator(story?.creator_profile || {});
    if (!draftDirty) {
      setDraft(draftFor(story?.current_generation));
      if (!lockedDirty) setLocked(Boolean(story?.current_generation?.review_state?.locked));
    }
  }, [story?.current_story_generation_uuid, story?.settings, story?.creator_profile, story?.current_generation, settingsDirty, creatorDirty, draftDirty, lockedDirty]);

  const generation = story?.current_generation;
  useEffect(() => {
    if (generation?.story_generation_uuid && appliedGenerationUuid && generation.story_generation_uuid !== appliedGenerationUuid) {
      setAppliedGenerationUuid("");
      setApplyUiState("idle");
    }
  }, [generation?.story_generation_uuid, appliedGenerationUuid]);
  const chapters = draft.chapters;
  async function saveSettings() {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy("settings");
    try {
      const expectedVersion = Number(settings.profile_version || 1);
      const result = await api.saveStorySettings(detail.project.id, settings, detail.project_revision, expectedVersion);
      if (!result.ok) throw new Error(result.error || "故事設定儲存失敗");
      await refreshProject({ forceFresh: true });
      setSettingsDirty(false);
      setMessage("故事設定已儲存；下次生成故事時才會套用新的 Profile。 ");
    } catch (error) {
      setMessage(`故事設定失敗：${formatApiError(error)}`);
    } finally {
      mutationControls.finishProjectMutation(mutation);
      setBusy("");
    }
  }

  async function saveCreator() {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy("creator");
    try {
      const expectedVersion = Number(creator.profile_version || 1);
      const result = await api.saveCreatorProfile(creator, expectedVersion);
      if (!result.ok) throw new Error(result.error || "Creator Profile 儲存失敗");
      await refreshProject({ forceFresh: true });
      setCreatorDirty(false);
      setMessage("Creator Profile 已儲存。");
    } catch (error) {
      setMessage(`Creator Profile 失敗：${formatApiError(error)}`);
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
      setDraftDirty(false);
      setLockedDirty(false);
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
    if (draftDirty || settingsDirty || creatorDirty || lockedDirty) {
      setMessage("請先儲存目前故事草稿與設定，再套用到既有分鏡。");
      return;
    }
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy("apply");
    setApplyUiState("post_apply_refresh_pending");
    try {
      const result = await api.applyStory(detail.project.id, generation.story_generation_uuid, detail.project_revision);
      if (!result.ok) {
        if (result.code === "stale_story_generation") {
          setApplyUiState("stale_before_apply");
          setMessage(`套用已拒絕：${result.error || "故事 generation 已過期，未修改既有分鏡。"}`);
          return;
        }
        setApplyUiState("apply_failed");
        throw new Error(result.error || "套用分鏡失敗");
      }
      setAppliedGenerationUuid(generation.story_generation_uuid);
      setApplyUiState("apply_succeeded");
      const successMessage = result.approval_invalidated ? "故事已套用到分鏡；輸出內容變更，請重新核准。" : "故事已套用到分鏡。";
      try {
        await refreshProject({ forceFresh: true, throwOnError: true });
        setApplyUiState("generation_archived_after_apply");
        setMessage(`${successMessage} 舊 generation 已歷史化，請查看最新專案與 review state。`);
      } catch (refreshError) {
        setApplyUiState("post_apply_refresh_failed");
        setMessage(`${refreshFailureMessage(successMessage, refreshError)}；Apply 已成功，請重新整理最新專案狀態。`);
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.payload.code === "stale_project_revision") {
        setApplyUiState("stale_before_apply");
        setMessage(`套用已拒絕：${formatApiError(error)}`);
      } else {
        setApplyUiState("apply_failed");
        setMessage(`套用分鏡失敗：${formatApiError(error)}`);
      }
    } finally {
      mutationControls.finishProjectMutation(mutation);
      setBusy("");
    }
  }

  function patchChapter(index: number, patch: Partial<StoryChapter>) {
    setDraftDirty(true);
    setDraft((current) => ({ ...current, chapters: current.chapters.map((chapter, chapterIndex) => chapterIndex === index ? { ...chapter, ...patch } : chapter) }));
  }

  function moveChapter(index: number, direction: -1 | 1) {
    setDraftDirty(true);
    setDraft((current) => {
      const nextIndex = index + direction;
      if (nextIndex < 0 || nextIndex >= current.chapters.length) return current;
      const chapters = [...current.chapters];
      [chapters[index], chapters[nextIndex]] = [chapters[nextIndex], chapters[index]];
      return { ...current, chapters };
    });
  }

  function patchCreator(patch: Record<string, unknown>) {
    setCreatorDirty(true);
    setCreator((current) => ({ ...current, ...patch }));
  }

  function moveSegment(chapterIndex: number, segmentIndex: number, direction: -1 | 1) {
    setDraftDirty(true);
    setDraft((current) => {
      const nextChapters = current.chapters.map((chapter) => ({ ...chapter, segment_uuids: [...(chapter.segment_uuids || [])] }));
      const chapter = nextChapters[chapterIndex];
      if (!chapter?.segment_uuids) return current;
      const nextSegmentIndex = segmentIndex + direction;
      if (nextSegmentIndex >= 0 && nextSegmentIndex < chapter.segment_uuids.length) {
        [chapter.segment_uuids[segmentIndex], chapter.segment_uuids[nextSegmentIndex]] = [chapter.segment_uuids[nextSegmentIndex], chapter.segment_uuids[segmentIndex]];
        return { ...current, chapters: nextChapters };
      }
      const targetChapter = nextChapters[chapterIndex + direction];
      if (!targetChapter?.segment_uuids || segmentIndex !== (direction < 0 ? 0 : chapter.segment_uuids.length - 1)) return current;
      const [segmentUuid] = chapter.segment_uuids.splice(segmentIndex, 1);
      if (segmentUuid) {
        if (direction < 0) targetChapter.segment_uuids.push(segmentUuid);
        else targetChapter.segment_uuids.unshift(segmentUuid);
      }
      return { ...current, chapters: nextChapters };
    });
  }

  function segmentFor(uuid: string) {
    return (detail.segments || []).find((segment) => segment.segment_id === uuid || (segment as SegmentWithUuid).segment_uuid === uuid);
  }

  async function updateCalibration(action: "recalculate" | "reset") {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy("calibration");
    try {
      const result = action === "reset"
        ? await api.resetStoryCalibration(detail.project.id, String(settings.profile_id || "general_diary"))
        : await api.recalculateStoryCalibration(detail.project.id, String(settings.profile_id || "general_diary"));
      if (!result.ok) throw new Error(result.error || "calibration 更新失敗");
      await refreshProject({ forceFresh: true });
      setMessage(action === "reset" ? "Calibration 已重設。" : "Calibration 已從 approved output 重新計算。");
    } catch (error) {
      setMessage(`Calibration 失敗：${formatApiError(error)}`);
    } finally {
      mutationControls.finishProjectMutation(mutation);
      setBusy("");
    }
  }

  const review = generation?.review_state || {};
  const normalized = generation?.normalized_response || {};
  const suppressed = review.suppressed_segments || normalized.suppressed_segments || [];
  const dirty = settingsDirty || creatorDirty || draftDirty || lockedDirty;
  const backendGenerationArchived = story?.generation_archived_after_apply === true
    || story?.current_generation_state === "generation_archived_after_apply"
    || Boolean(generation?.applied_at);
  const applySucceededForCurrentGeneration = Boolean(
    generation
    && (backendGenerationArchived || appliedGenerationUuid === generation.story_generation_uuid)
    && applyUiState !== "apply_failed"
    && applyUiState !== "stale_before_apply",
  );
  const generationArchivedAfterApply = backendGenerationArchived
    || (applySucceededForCurrentGeneration && applyUiState !== "post_apply_refresh_pending");
  const staleBeforeApply = Boolean(
    story?.current_generation_is_stale
    && !generationArchivedAfterApply
    && story?.current_generation_state !== "generation_archived_after_apply",
  );
  const applyBlocked = Boolean(busy) || staleBeforeApply || applySucceededForCurrentGeneration;

  return <section className="story-understanding card" aria-label="專案故事理解" data-unsaved-text-draft={dirty ? "true" : undefined}>
    <div className="story-understanding-heading">
      <div><span className="eyebrow">PROJECT STORY UNDERSTANDING</span><h3>故事理解與人工審核</h3><p>故事生成只提供建議，必須明確套用到既有分鏡後才會改變剪輯流程。</p></div>
      <div className="row">
        <button type="button" disabled={Boolean(busy)} onClick={() => void saveSettings()}>儲存設定</button>
        <button type="button" disabled={Boolean(busy)} onClick={() => void saveCreator()}>儲存 Creator</button>
        <button type="button" className="primary" disabled={Boolean(busy)} onClick={() => void generate()}>{busy === "generate" ? "生成中…" : "生成故事"}</button>
        {generation && <button type="button" disabled={Boolean(busy)} onClick={() => void generate(true)}>重新生成</button>}
      </div>
    </div>
    <div className="story-settings-grid">
      <label>Story Profile<select value={String(settings.profile_id || "general_diary")} disabled={Boolean(busy)} onChange={(event) => { setSettingsDirty(true); setSettings({ ...settings, profile_id: event.target.value }); }}>
        <option value="travel_diary">travel_diary｜旅行日記</option><option value="coffee_matcha_diary">coffee_matcha_diary｜咖啡／抹茶日記</option><option value="roasting_diary">roasting_diary｜烘豆日記</option><option value="general_diary">general_diary｜一般日記</option>
      </select></label>
      <label>文字語言<input value={String(creator.language || "zh-TW")} disabled={Boolean(busy)} onChange={(event) => patchCreator({ language: event.target.value })} /></label>
      <label>字卡密度<select value={String(creator.title_card_density || "low")} disabled={Boolean(busy)} onChange={(event) => patchCreator({ title_card_density: event.target.value })}><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label>
      <label>節奏偏好<input value={String(settings.desired_pacing || "")} disabled={Boolean(busy)} placeholder="例如：自然、慢節奏、保留等待" onChange={(event) => { setSettingsDirty(true); setSettings({ ...settings, desired_pacing: event.target.value }); }} /></label>
      <label className="wide">Creator wording style<input value={String(creator.wording_style || "")} disabled={Boolean(busy)} onChange={(event) => patchCreator({ wording_style: event.target.value })} /></label>
      <label className="wide">Creator visual style<input value={String(creator.visual_style || "")} disabled={Boolean(busy)} onChange={(event) => patchCreator({ visual_style: event.target.value })} /></label>
      <label className="wide">Creator transition preference<input value={String(creator.transition_preference || "")} disabled={Boolean(busy)} onChange={(event) => patchCreator({ transition_preference: event.target.value })} /></label>
      <label className="wide">Creator natural audio policy<textarea value={String(creator.natural_audio_policy || "")} disabled={Boolean(busy)} onChange={(event) => patchCreator({ natural_audio_policy: event.target.value })} /></label>
      <label className="wide">不喜歡的風格<input value={Array.isArray(creator.disliked_styles) ? creator.disliked_styles.join(", ") : String(creator.disliked_styles || "")} disabled={Boolean(busy)} onChange={(event) => patchCreator({ disliked_styles: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) })} /></label>
      <label className="creator-check"><input type="checkbox" checked={Boolean(creator.tutorial_tone_allowed)} disabled={Boolean(busy)} onChange={(event) => patchCreator({ tutorial_tone_allowed: event.target.checked })} />允許教學語氣</label>
      <label className="creator-check"><input type="checkbox" checked={Boolean(creator.sponsored_tone_allowed)} disabled={Boolean(busy)} onChange={(event) => patchCreator({ sponsored_tone_allowed: event.target.checked })} />允許業配語氣</label>
      <div className="creator-readonly" aria-label="Creator profile metadata"><span>schema version：{String(creator.schema_version || 1)}</span><span>profile version：{String(creator.profile_version || 1)}</span><span>calibration：{JSON.stringify(creator.calibration || {})}</span></div>
      <label className="wide">專案故事意圖<textarea value={String(settings.project_intent || "")} disabled={Boolean(busy)} placeholder="這支影片想讓觀眾感受到什麼？" onChange={(event) => { setSettingsDirty(true); setSettings({ ...settings, project_intent: event.target.value }); }} /></label>
      <label className="wide">行程／場景順序<textarea value={String(settings.itinerary || "")} disabled={Boolean(busy)} placeholder="例如：早上車站，下午展覽，傍晚咖啡廳" onChange={(event) => { setSettingsDirty(true); setSettings({ ...settings, itinerary: event.target.value }); }} /></label>
      <label className="wide">期望順序<input value={(settings.desired_sequence || []).join(", ")} disabled={Boolean(busy)} onChange={(event) => { setSettingsDirty(true); setSettings({ ...settings, desired_sequence: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) }); }} /></label>
      <label className="wide">必留內容<input value={(settings.must_keep || []).join(", ")} disabled={Boolean(busy)} onChange={(event) => { setSettingsDirty(true); setSettings({ ...settings, must_keep: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) }); }} /></label>
      <label className="wide">排除指引<input value={(settings.exclude_guidance || []).join(", ")} disabled={Boolean(busy)} onChange={(event) => { setSettingsDirty(true); setSettings({ ...settings, exclude_guidance: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) }); }} /></label>
      <label>字卡偏好<input value={String(settings.title_card_preference_override || "")} disabled={Boolean(busy)} onChange={(event) => { setSettingsDirty(true); setSettings({ ...settings, title_card_preference_override: event.target.value }); }} /></label>
      <label>自然音偏好<input value={String(settings.natural_audio_override || "")} disabled={Boolean(busy)} onChange={(event) => { setSettingsDirty(true); setSettings({ ...settings, natural_audio_override: event.target.value }); }} /></label>
    </div>
    {!generation && <p className="inline-empty">尚未生成故事。請先儲存設定，再按「生成故事」。</p>}
    {generation && <>
      <div className="story-generation-meta"><b>{generation.story_generation_uuid}</b><span>{generation.provider} / {generation.model}</span><span>input {generation.input_hash?.slice(0, 12)}</span><span>狀態：{generation.status}</span></div>
      {staleBeforeApply && <p className="story-stale" role="alert">目前 StoryInputSnapshot 已變更；此 generation 在 Apply 前已過期，操作會 fail closed，未修改既有分鏡。請重新生成。</p>}
      {generationArchivedAfterApply && <p className="story-success" role="status">此 generation 已成功套用並歷史化；請查看最新 project 與 review state。若輸出內容已變更，需重新核准。</p>}
      {applyUiState === "post_apply_refresh_pending" && <p className="story-success" role="status">Apply 已成功，正在更新最新 project 與 review state…</p>}
      {applyUiState === "post_apply_refresh_failed" && <p className="story-stale" role="alert">Apply 已成功，但最新 project / review state 尚未載入；請重新整理，不會自動重送 Apply。</p>}
      <div className="story-audit" aria-label="故事 audit"><span>raw provider calls：{generation.provider_audit?.calls ?? "—"}</span><span>corrective retry：{generation.provider_audit?.retries ?? 0}</span><span>latency：{generation.provider_audit?.total_latency_ms ?? "—"} ms</span><span>validation：{String(generation.validation?.status || "unknown")}</span><span>effective source：{generation.story_audit?.effective?.source || "normalized"}</span></div>
      <div className="story-audit-details" aria-label="raw normalized effective audit">
        <details><summary>Raw provider audit</summary><pre>{JSON.stringify(generation.story_audit?.raw || { provider: generation.provider, model: generation.model, input_hash: generation.input_hash }, null, 2)}</pre></details>
        <details><summary>Normalized output audit</summary><pre>{JSON.stringify(generation.story_audit?.normalized || { chapter_count: normalized.chapters?.length || 0 }, null, 2)}</pre></details>
        <details><summary>Effective review audit</summary><pre>{JSON.stringify(generation.story_audit?.effective || { source: review.source || "normalized", locked: Boolean(review.locked) }, null, 2)}</pre></details>
      </div>
      <label className="wide">專案摘要<textarea value={draft.project_summary} disabled={Boolean(busy)} onChange={(event) => { setDraftDirty(true); setDraft({ ...draft, project_summary: event.target.value }); }} /></label>
      <div className="story-chapters">
        {chapters.map((chapter, index) => <article className="story-chapter" key={chapter.chapter_id || index}>
          <div className="story-chapter-head"><strong>章節 {index + 1}</strong><span>信心 {Math.round((chapter.confidence || 0) * 100)}% · {chapter.locked ? "已鎖定" : "未鎖定"}</span></div>
          <label>標題<input value={chapter.title || ""} disabled={Boolean(busy)} onChange={(event) => patchChapter(index, { title: event.target.value })} /></label>
          <label>目的<textarea value={chapter.purpose || ""} disabled={Boolean(busy)} onChange={(event) => patchChapter(index, { purpose: event.target.value })} /></label>
          <label>片段順序<textarea value={(chapter.segment_uuids || []).join(", ")} disabled={Boolean(busy)} onChange={(event) => patchChapter(index, { segment_uuids: event.target.value.split(",").map((id) => id.trim()).filter(Boolean) })} /></label>
          <label>節奏<input value={chapter.pacing_intent || ""} disabled={Boolean(busy)} onChange={(event) => patchChapter(index, { pacing_intent: event.target.value })} /></label>
          <label>轉場意圖<input value={chapter.transition_intent || ""} disabled={Boolean(busy)} onChange={(event) => patchChapter(index, { transition_intent: event.target.value })} /></label>
          <label>標題字卡建議<textarea value={chapter.title_card_suggestion || ""} disabled={Boolean(busy)} onChange={(event) => patchChapter(index, { title_card_suggestion: event.target.value })} /></label>
          <label>自然音<input value={chapter.natural_audio_intent || ""} disabled={Boolean(busy)} onChange={(event) => patchChapter(index, { natural_audio_intent: event.target.value })} /></label>
          <label>章節備註<textarea value={chapter.notes || ""} disabled={Boolean(busy)} onChange={(event) => patchChapter(index, { notes: event.target.value })} /></label>
          <div className="story-segment-cards" aria-label={`章節 ${index + 1} 視覺片段 cards`}>
            {(chapter.segment_uuids || []).map((segmentUuid, segmentIndex) => {
              const segment = segmentFor(segmentUuid);
              const start = Number(segment?.start_seconds || 0);
              const end = Number(segment?.end_seconds || start + 1);
              return <article className="story-segment-card" data-segment-card={segmentUuid} key={segmentUuid}>
                {segment?.media_url ? <video muted preload="metadata" src={`${segment.media_url}#t=${start},${end}`} aria-label={`${segmentUuid} preview`} /> : <div className="story-segment-placeholder" aria-label={`${segmentUuid} preview`}>預覽未提供</div>}
                <div className="story-segment-card-copy"><strong>{segment?.title || segmentUuid}</strong><span>{segmentUuid} · {start.toFixed(2)}s–{end.toFixed(2)}s</span><span>{segment?.scene_role || "未指定 shot role"}</span></div>
                <div className="row"><button type="button" aria-label={`片段 ${segmentUuid} 上移`} disabled={Boolean(busy)} onClick={() => moveSegment(index, segmentIndex, -1)}>片段上移</button><button type="button" aria-label={`片段 ${segmentUuid} 下移`} disabled={Boolean(busy)} onClick={() => moveSegment(index, segmentIndex, 1)}>片段下移</button></div>
              </article>;
            })}
          </div>
          <div className="row"><button type="button" disabled={Boolean(busy) || index === 0} onClick={() => moveChapter(index, -1)}>章節上移</button><button type="button" disabled={Boolean(busy) || index === chapters.length - 1} onClick={() => moveChapter(index, 1)}>章節下移</button><button type="button" disabled={Boolean(busy)} onClick={() => patchChapter(index, { locked: !chapter.locked })}>{chapter.locked ? "解除本章鎖定" : "鎖定本章"}</button></div>
        </article>)}
      </div>
      <div className="story-suppression" aria-label="duplicate suppression evidence">
        <strong>Duplicate suppression evidence</strong>
        {suppressed.length === 0 ? <span>目前沒有被抑制的 duplicate segment。</span> : suppressed.map((item) => <span key={`${item.segment_uuid}:${item.representative_segment_uuid}`}>{item.segment_uuid} → representative {item.representative_segment_uuid}（{item.reason || "duplicate"}）</span>)}
      </div>
      <div className="row story-actions">
        <button type="button" disabled={Boolean(busy)} onClick={() => void saveReview()}>{busy === "review" ? "儲存中…" : "儲存故事修改"}</button>
        <button type="button" disabled={Boolean(busy)} onClick={() => { setLockedDirty(true); setLocked((value) => !value); }}>{locked ? "解除鎖定故事" : "鎖定故事修改"}</button>
        <button type="button" className="primary" disabled={applyBlocked} onClick={() => void applyToStoryboard()}>{busy === "apply" ? "套用中…" : staleBeforeApply ? "不可套用：generation 已過期" : applySucceededForCurrentGeneration ? "已套用；generation 已歷史化" : "套用到既有分鏡"}</button>
      </div>
      <p className="muted">套用前不會修改 storyboard.json、approval 或正式輸出；鎖定的既有分鏡片段會在套用時保留。</p>
    </>}
    <div className="story-calibration" aria-label="Story calibration">
      <div><strong>Calibration</strong><span>來源：{story?.calibration?.source || "approved outputs only"}</span><span>樣本：{story?.calibration?.sample_count ?? 0}</span><span>狀態：{story?.calibration?.status || "insufficient_data"}</span></div>
      <div className="row"><button type="button" disabled={Boolean(busy)} onClick={() => void updateCalibration("recalculate")}>重新計算</button><button type="button" disabled={Boolean(busy)} onClick={() => void updateCalibration("reset")}>重設</button></div>
    </div>
  </section>;
}
