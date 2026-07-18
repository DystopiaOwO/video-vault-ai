from __future__ import annotations

COFFEE_TAGS = ["coffee", "pour_over", "kettle", "dripping", "closeup", "calm", "asmr"]
TRAVEL_TAGS = ["travel", "street", "food", "cafe", "landscape", "walking", "city"]
FRAME_TAGS = ["coffee", "food", "closeup", "hands", "dripping"]


class MockProvider:
    provider = "mock"
    model = "rules"
    prompt_version = "perception_zh_tw_v1"

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
