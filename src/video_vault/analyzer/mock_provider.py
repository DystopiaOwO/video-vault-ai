from __future__ import annotations

COFFEE_TAGS = ["coffee", "pour_over", "kettle", "dripping", "closeup", "calm", "asmr"]
TRAVEL_TAGS = ["travel", "street", "food", "cafe", "landscape", "walking", "city"]
FRAME_TAGS = ["coffee", "food", "closeup", "hands", "dripping"]


class MockProvider:
    provider = "mock"
    model = "rules"

    def analyze(self, video: dict) -> dict:
        duration = float(video.get("duration_seconds") or 60)
        category = video.get("category") or "unknown"
        tags = TRAVEL_TAGS if category == "travel" else COFFEE_TAGS
        kind = "highlight" if category == "travel" else "b_roll"
        return {
            "summary": f"Mock analysis for {video['filename']} in category {category}.",
            "tags": tags,
            "segments": [
                {
                    "start_seconds": min(5, duration),
                    "end_seconds": min(35, duration),
                    "segment_type": "shorts",
                    "title": "Short candidate",
                    "reason": "Stable 30 second window for quick review.",
                    "tags": tags[:4],
                    "score": 0.78,
                    "suggested_use": "Shorts",
                },
                {
                    "start_seconds": min(40, duration),
                    "end_seconds": min(70, duration),
                    "segment_type": kind,
                    "title": "B-roll candidate",
                    "reason": "Rule-based placeholder until vision analysis is wired.",
                    "tags": tags[2:6],
                    "score": 0.66,
                    "suggested_use": "B-roll",
                },
            ],
        }

    def analyze_frame(self, frame_path, timestamp: float, video: dict) -> tuple[dict, dict]:
        tags = FRAME_TAGS if int(timestamp) % 10 == 0 else ["coffee", "closeup"]
        result = {
            "summary": f"Mock frame analysis at {timestamp:.0f}s.",
            "tags": tags,
            "visual_quality_score": 0.72,
            "usefulness_score": 0.8 if "hands" in tags or "dripping" in tags else 0.55,
            "suggested_use": "Shorts" if "hands" in tags or "dripping" in tags else "B-roll",
        }
        return result, {"provider": self.provider, "model": self.model, "mock": True}
