import { useEffect, useRef, useState } from "react";
import { api, type Clip, type Job } from "../../api";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import { createProjectMutationControls, mutationLabel, ProjectMutationCoordinator, type ProjectMutationControls } from "../../projectMutation";

export type ClipSummaryEditorProps = {
  projectId: number;
  projectRevision?: number;
  clip: Pick<Clip, "video_id" | "filename" | "ai_visual_summary" | "user_summary" | "user_summary_migration_state">;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]>;
  mutationControls?: ProjectMutationControls;
};

export function ClipSummaryEditor({ projectId, projectRevision, clip, setMessage, refreshProject, mutationControls }: ClipSummaryEditorProps) {
  const incomingSummary = clip.user_summary || "";
  const aiSummary = clip.ai_visual_summary || "";
  const [text, setText] = useState(incomingSummary);
  const [baseline, setBaseline] = useState(incomingSummary);
  const [saving, setSaving] = useState(false);
  const identityRef = useRef(`${projectId}:${clip.video_id}`);
  const dirty = text !== baseline;
  const dirtyRef = useRef(dirty);
  const fallbackControlsRef = useRef<ProjectMutationControls | null>(null);
  if (!fallbackControlsRef.current) fallbackControlsRef.current = createProjectMutationControls(new ProjectMutationCoordinator());
  const controls = mutationControls || fallbackControlsRef.current;
  const projectMutationBusy = controls.isProjectMutationBusy(projectId);
  const editorId = `clip-user-summary-${projectId}-${clip.video_id}`;

  function setProjectMessage(message: string) {
    if (controls.isCurrentProject(projectId)) setMessage(message);
  }

  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => {
    const identity = `${projectId}:${clip.video_id}`;
    const identityChanged = identityRef.current !== identity;
    if (identityChanged) identityRef.current = identity;
    if (!identityChanged && dirtyRef.current) return;

    setText(incomingSummary);
    setBaseline(incomingSummary);
    setSaving(false);
  }, [clip.video_id, incomingSummary, projectId]);

  useEffect(() => {
    if (!dirty) return;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [dirty]);

  async function save() {
    const mutation = controls.beginProjectMutation(projectId, "clip-summary");
    if (!mutation) {
      setProjectMessage(`目前正在${mutationLabel("clip-summary")}，請完成後再執行其他操作。`);
      return;
    }
    const summary = text.trim();
    setSaving(true);
    setProjectMessage("正在儲存使用者故事備註並重建故事整理...");
    try {
      const result = await (projectRevision === undefined
        ? api.saveClipSummary(projectId, clip.video_id, summary)
        : api.saveClipSummary(projectId, clip.video_id, summary, projectRevision));
      if (!result.ok) {
        setProjectMessage("使用者故事備註儲存失敗：找不到素材。");
        return;
      }
      setText(summary);
      setBaseline(summary);
      const successMessage = "使用者故事備註已儲存，故事整理已重建並回到待審。";
      setProjectMessage(successMessage);
      try {
        await refreshProject({ forceFresh: true, throwOnError: true });
      } catch (error) {
        if (controls.isCurrentProject(projectId)) {
          setProjectMessage(`${successMessage}，但畫面更新失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
        }
      }
    } catch (error) {
      if (controls.isCurrentProject(projectId)) {
        setProjectMessage(`使用者故事備註儲存失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
      }
    } finally {
      setSaving(false);
      controls.finishProjectMutation(mutation);
    }
  }

  return <div className="clip-summary-editor" data-unsaved-text-draft={dirty ? "true" : undefined}>
    <section className="clip-ai-summary" aria-label={`${clip.filename} AI 畫面感知`}>
      <div className="clip-summary-heading">
        <strong>AI 畫面感知</strong>
        <span>唯讀，重新感知時更新</span>
      </div>
      <p>{aiSummary || "尚未產生 AI 畫面感知。"}</p>
    </section>
    <div className="clip-summary-heading">
      <label htmlFor={editorId}>使用者故事備註</label>
      {dirty && <span role="status">有未儲存變更</span>}
    </div>
    <textarea
      id={editorId}
      aria-label={`${clip.filename} 使用者故事備註`}
      value={text}
      onChange={(event) => setText(event.target.value)}
      placeholder="例如：這段是抵達飯店，不要放在早餐章節"
      disabled={saving || projectMutationBusy}
    />
    <small>儲存後會重新產生故事整理；不會修改 AI 的逐幀感知。</small>
    {clip.user_summary_migration_state === "review" && <p role="note">舊版描述與所有 AI frame 內容相同，無法安全判斷來源，請確認後重新儲存。</p>}
    <div className="clip-summary-actions">
      <small>{text.length} 字</small>
      <button type="button" disabled={!dirty || saving || projectMutationBusy} onClick={() => setText(baseline)}>放棄變更</button>
      <button type="button" className="good" disabled={!dirty || saving || projectMutationBusy} onClick={() => void save()}>{saving ? "儲存中…" : "儲存故事備註"}</button>
    </div>
  </div>;
}
