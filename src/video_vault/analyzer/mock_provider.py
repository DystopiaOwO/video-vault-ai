from __future__ import annotations

from .multi_frame import MULTI_FRAME_PROMPT_VERSION

COFFEE_TAGS = ["coffee", "pour_over", "kettle", "dripping", "closeup", "calm", "asmr"]
TRAVEL_TAGS = ["travel", "street", "food", "cafe", "landscape", "walking", "city"]
FRAME_TAGS = ["coffee", "food", "closeup", "hands", "dripping"]


class MockProvider:
    provider = "mock"
    prompt_version = "perception_zh_tw_v1"
    supports_multi_frame = True
    multi_frame_max_images = 5
    multi_frame_prompt_version = MULTI_FRAME_PROMPT_VERSION

    def __init__(self, cfg: dict | None = None):
        mock = ((cfg or {}).get("ai") or {}).get("mock") or {}
        self.model = str(mock.get("model") or "rules")

    def analyze(self, video: dict) -> dict:
        duration = float(video.get("duration_seconds") or 60)
        category = video.get("category") or "unknown"
        tags = TRAVEL_TAGS if category == "travel" else COFFEE_TAGS
        kind = "highlight" if category == "travel" else "b_roll"
        return {
            "summary": f"{video['filename']} 的初步內容感知，分類為 {category}。",
            "tags": tags,
            "segments": [
                {
                    "start_seconds": min(5, duration),
                    "end_seconds": min(35, duration),
                    "segment_type": "shorts",
                    "title": "短影音候選片段",
                    "reason": "畫面時間長度適合先做快速檢查。",
                    "tags": tags[:4],
                    "score": 0.78,
                    "suggested_use": "短影音",
                },
                {
                    "start_seconds": min(40, duration),
                    "end_seconds": min(70, duration),
                    "segment_type": kind,
                    "title": "補畫面候選片段",
                    "reason": "規則式初步判斷，適合當作故事銜接或補充畫面。",
                    "tags": tags[2:6],
                    "score": 0.66,
                    "suggested_use": "補畫面",
                },
            ],
        }

    def analyze_frame(self, frame_path, timestamp: float, video: dict) -> tuple[dict, dict]:
        tags = FRAME_TAGS if int(timestamp) % 10 == 0 else ["coffee", "closeup"]
        result = {
            "summary": f"{timestamp:.0f} 秒附近是咖啡相關畫面，主體清楚，適合初步整理。",
            "tags": tags,
            "visual_quality_score": 0.72,
            "usefulness_score": 0.8 if "hands" in tags or "dripping" in tags else 0.55,
            "suggested_use": "短影音" if "hands" in tags or "dripping" in tags else "補畫面",
        }
        return result, {"provider": self.provider, "model": self.model, "mock": True}

    def analyze_window(self, frame_paths, timestamps: list[float], video: dict) -> tuple[dict, dict]:
        """Return a deterministic clip-level result without a single-frame fallback."""

        start = float(timestamps[0])
        end = float(timestamps[-1])
        category = str(video.get("category") or "unknown")
        tags = ["travel", "landscape"] if category == "travel" else ["coffee", "hands", "dripping"]
        result = {
            "summary": f"{start:.1f} 到 {end:.1f} 秒的{category}連續畫面。",
            "action": "連續動作與場景變化",
            "start_seconds": start,
            "end_seconds": end,
            "shot_role": "process" if category != "travel" else "context",
            "technical_quality": {"score": 0.82, "issues": []},
            "duplicate_group": f"{category or 'unknown'}_{int(start // 10)}",
            "natural_audio_recommendation": "keep",
            "confidence": 0.78,
            "tags": tags,
        }
        return result, {
            "provider": self.provider,
            "model": self.model,
            "mock": True,
            "frame_count": len(frame_paths),
            "timestamps": [float(value) for value in timestamps],
        }
