from __future__ import annotations

from pathlib import Path


path = Path("web/tests/pr52-gate1-final.test.tsx")
text = path.read_text(encoding="utf-8")
old_clip = '''    clips: [{ clip_id: "clip-1", video_id: 1, filename: "clip-1.mp4", status: "perceived", segment_count: 1, duration_seconds: 12, detected_category: "travel", time_of_day: "morning", visual_summary: "車站入口" }],
'''
new_clip = '''    clips: [{
      clip_id: "clip-1",
      video_id: 1,
      filename: "clip-1.mp4",
      status: "perceived",
      segment_count: 1,
      duration_seconds: 12,
      detected_category: "travel",
      time_of_day: "morning",
      visual_summary: "車站入口",
      ai_visual_summary: "車站入口",
      user_summary: "",
      user_summary_updated_at: null,
      user_summary_migration_state: "native",
      effective_summary: "車站入口",
      effective_summary_source: "ai",
    }],
'''
if old_clip not in text:
    if new_clip not in text:
        raise SystemExit("project detail clip fixture not found")
else:
    text = text.replace(old_clip, new_clip, 1)
text = text.replace(
    'screen.getByLabelText("clip-1.mp4 內容感知描述")',
    'screen.getByLabelText("clip-1.mp4 使用者故事備註")',
)
text = text.replace(
    '{ target: { value: "新的內容感知描述" } }',
    '{ target: { value: "新的使用者故事備註" } }',
)
text = text.replace(
    'screen.getByRole("button", { name: "儲存描述" })',
    'screen.getByRole("button", { name: "儲存故事備註" })',
)
path.write_text(text, encoding="utf-8")
