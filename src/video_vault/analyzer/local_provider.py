from __future__ import annotations

from pathlib import Path
import base64
import json
import urllib.request
from urllib.error import HTTPError

from .frame_analysis import TAGS


class LocalProvider:
    provider = "local"

    def __init__(self, cfg: dict | None = None):
        local = (cfg or {}).get("ai", {}).get("local", {})
        self.base_url = str(local.get("base_url") or local.get("lmstudio_url") or "http://127.0.0.1:1234/v1").rstrip("/")
        self.model = str(local.get("model") or "local-vision")

    def analyze_frame(self, frame_path: Path, timestamp: float, video: dict) -> tuple[dict, dict]:
        raw = self._post(self._request_body(frame_path, timestamp, video))
        return _parse(raw), raw

    def _request_body(self, frame_path: Path, timestamp: float, video: dict) -> dict:
        image = base64.b64encode(frame_path.read_bytes()).decode("ascii")
        prompt = (
            "Analyze one sampled video frame for a video editor. Return strict JSON only: "
            '{"summary": string, "tags": string[], "visual_quality_score": number, '
            '"usefulness_score": number, "suggested_use": string}. '
            f"Use only these tags: {', '.join(TAGS)}. Do not guess tags that are not visible. "
            "coffee means coffee gear, beans, espresso, pour-over, cup, kettle, dripper, or cafe scene. "
            "matcha means green tea powder, whisk, bowl, or matcha drink. "
            "roasting means beans, roaster, roasting machine, smoke, or roast process. "
            "travel means street, vehicle, walking, city, destination, or trip context. "
            "food means edible food or drinks. landscape means wide outdoor/scenery view. "
            "closeup means subject fills the frame. hands means visible hands. "
            "steam means visible vapor. dripping means visible liquid drip/pour. "
            "Score usefulness higher for clear action, stable framing, closeups, hands, steam, dripping, or strong establishing shots; lower for blur, darkness, overexposure, or unclear frames. "
            f"Filename: {video.get('filename')}; timestamp: {timestamp:.1f}s."
        )
        return {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
                    ],
                }
            ],
        }

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as res:
                return json.loads(res.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"local model API failed: HTTP {exc.code}; {detail}") from exc


def _parse(raw: dict) -> dict:
    text = raw["choices"][0]["message"]["content"]
    text = text.strip().removeprefix("```json").removesuffix("```").strip()
    data = json.loads(text)
    tags = [tag for tag in data.get("tags", []) if tag in TAGS]
    return {
        "summary": str(data.get("summary", "")),
        "tags": tags,
        "visual_quality_score": _score(data.get("visual_quality_score", 0)),
        "usefulness_score": _score(data.get("usefulness_score", 0)),
        "suggested_use": str(data.get("suggested_use", "B-roll")),
    }


def _score(value: object) -> float:
    score = float(value or 0)
    if score > 1:
        score = score / 10
    return max(0.0, min(1.0, score))
