from __future__ import annotations

from pathlib import Path
import base64
import json
import os
import time
import urllib.request

from .frame_analysis import TAGS


class CloudProvider:
    provider = "cloud"

    def __init__(self, cfg: dict):
        cloud = cfg.get("ai", {}).get("cloud", {})
        self.model = cloud.get("model") or "gpt-4.1-mini"
        self.api_key = os.environ.get(cloud.get("api_key_env", "OPENAI_API_KEY"), "")

    def analyze_frame(self, frame_path: Path, timestamp: float, video: dict) -> tuple[dict, dict]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        body = self._request_body(frame_path, timestamp, video)
        last_error = None
        for attempt in range(3):
            try:
                raw = self._post(body)
                return _parse(raw), raw
            except Exception as exc:
                last_error = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"vision API failed after retries: {last_error}")

    def _request_body(self, frame_path: Path, timestamp: float, video: dict) -> dict:
        image = base64.b64encode(frame_path.read_bytes()).decode("ascii")
        prompt = (
            "Analyze this single video frame. Return JSON only with keys: "
            "summary string, tags array, visual_quality_score number 0-1, "
            "usefulness_score number 0-1, suggested_use string. "
            f"Allowed tags: {', '.join(TAGS)}. "
            f"Video filename: {video.get('filename')}; timestamp: {timestamp:.1f}s."
        )
        return {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image}"},
                    ],
                }
            ],
        }

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read().decode("utf-8"))


def _parse(raw: dict) -> dict:
    text = raw.get("output_text", "")
    if not text:
        for item in raw.get("output", []):
            for content in item.get("content", []):
                text += content.get("text", "")
    data = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
    tags = [tag for tag in data.get("tags", []) if tag in TAGS]
    return {
        "summary": str(data.get("summary", "")),
        "tags": tags,
        "visual_quality_score": float(data.get("visual_quality_score", 0)),
        "usefulness_score": float(data.get("usefulness_score", 0)),
        "suggested_use": str(data.get("suggested_use", "B-roll")),
    }
