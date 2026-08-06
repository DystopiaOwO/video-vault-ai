from __future__ import annotations

from pathlib import Path
import base64
import json
import os
import time
import urllib.request

from .frame_analysis import PROMPT_VERSION, TAGS, has_cjk
from .multi_frame import MULTI_FRAME_PROMPT_VERSION, parse_window_response


class CloudProvider:
    provider = "cloud"
    prompt_version = PROMPT_VERSION
    supports_multi_frame = True
    multi_frame_max_images = 5
    multi_frame_prompt_version = MULTI_FRAME_PROMPT_VERSION

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
                parsed = _parse(raw)
                if not has_cjk(parsed["summary"] + parsed["suggested_use"]):
                    raw = self._post(self._request_body(frame_path, timestamp, video, "你剛剛回英文了。請重新輸出同一個 JSON，但 summary 與 suggested_use 必須全部使用繁體中文。"))
                    parsed = _parse(raw)
                return parsed, raw
            except Exception as exc:
                last_error = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"vision API failed after retries: {last_error}")

    def analyze_window(self, frame_paths: list[Path], timestamps: list[float], video: dict) -> tuple[dict, dict]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        body = self._window_request_body(frame_paths, timestamps, video)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                raw = self._post(body)
                return parse_window_response(raw), raw
            except Exception as exc:
                last_error = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"vision multi-frame API failed after retries: {last_error}") from last_error

    def _request_body(self, frame_path: Path, timestamp: float, video: dict, extra: str = "") -> dict:
        image = base64.b64encode(frame_path.read_bytes()).decode("ascii")
        prompt = (
            "請分析這張從影片抽出的單一畫面。只回傳 JSON，欄位包含："
            "summary string, tags array, visual_quality_score number 0-1, "
            "usefulness_score number 0-1, suggested_use string。"
            "summary 必須用繁體中文描述畫面；suggested_use 必須用繁體中文，例如：短影音、補畫面、產品特寫、轉場、開場。"
            f"tags 必須維持英文，且只能使用：{', '.join(TAGS)}。"
            f"Video filename: {video.get('filename')}; timestamp: {timestamp:.1f}s. {extra}"
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

    def _window_request_body(self, frame_paths: list[Path], timestamps: list[float], video: dict) -> dict:
        prompt = (
            "請分析以下同一影片區段的連續畫面，作為一個 multi-frame 感知單位。只回傳 JSON："
            '{"summary": string, "action": string, "start_seconds": number, "end_seconds": number, '
            '"shot_role": string, "technical_quality": {"score": number, "issues": string[]}, '
            '"duplicate_group": string, "natural_audio_recommendation": "keep|lower|mute|unknown", '
            '"confidence": number, "tags": string[]}. '
            "時間範圍必須落在提供的 timestamps 內，不要自行重抽影格或合併其他區段。"
            f"tags 只能使用：{', '.join(TAGS)}；summary、action、shot_role、issues 使用繁體中文。"
            f"Video filename: {video.get('filename')}; timestamps: {', '.join(f'{value:.3f}' for value in timestamps)}。"
        )
        content = [{"type": "input_text", "text": prompt}]
        for frame_path in frame_paths:
            image = base64.b64encode(frame_path.read_bytes()).decode("ascii")
            content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{image}"})
        return {
            "model": self.model,
            "input": [{"role": "user", "content": content}],
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
        "suggested_use": str(data.get("suggested_use", "補畫面")),
    }
