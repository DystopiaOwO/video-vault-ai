from __future__ import annotations

from pathlib import Path


path = Path("src/video_vault/project.py")
text = path.read_text(encoding="utf-8")
old = '''    for group in ordered:
        group["clips"] = _dedupe(group["clips"])
        group["story_context"] = _dedupe_story_context(group["story_context"])
        group["segments"].sort(key=lambda s: (s["clip_id"], float(s["start_seconds"] or 0)) if itinerary or project_info_is_travel(row) else (-float(s["score"] or 0), s["clip_id"], float(s["start_seconds"] or 0)))
    project_info = dict(row)
'''
new = '''    for group in ordered:
        group["clips"] = _dedupe(group["clips"])
        group["story_context"] = _dedupe_story_context(group["story_context"])
        group["segments"].sort(key=lambda s: (s["clip_id"], float(s["start_seconds"] or 0)) if itinerary or project_info_is_travel(row) else (-float(s["score"] or 0), s["clip_id"], float(s["start_seconds"] or 0)))
    story_context_usage = _dedupe_story_context(
        [
            context
            for group in ordered
            for context in group.get("story_context", [])
            if context.get("user_summary")
        ]
    )
    project_info = dict(row)
'''
if old not in text:
    if new not in text:
        raise SystemExit("ordered group provenance target not found")
else:
    text = text.replace(old, new, 1)
old_usage = '''        "story_context_usage": [
            _story_context_usage(c, story_context(c["user_summary"], c["ai_visual_summary"], "其他"))
            for c in clips if c.get("user_summary")
        ],
'''
new_usage = '''        "story_context_usage": story_context_usage,
'''
if old_usage not in text:
    if new_usage not in text:
        raise SystemExit("story_context_usage target not found")
else:
    text = text.replace(old_usage, new_usage, 1)
old_feedback = '''        "feedback_applied": [
            f"{item['clip_id']} 使用 user_summary 指引故事分組"
            for item in [
                _story_context_usage(c, story_context(c["user_summary"], c["ai_visual_summary"], "其他"))
                for c in clips if c.get("user_summary")
            ]
        ],
'''
new_feedback = '''        "feedback_applied": [
            f"{item['clip_id']} 使用 user_summary 指引故事分組"
            for item in story_context_usage
            if item.get("guidance_applied")
        ],
'''
if old_feedback not in text:
    if new_feedback not in text:
        raise SystemExit("feedback_applied target not found")
else:
    text = text.replace(old_feedback, new_feedback, 1)
path.write_text(text, encoding="utf-8")
