from __future__ import annotations

from pathlib import Path
import base64
import json
import shutil
import subprocess
import time
import urllib.request
from urllib.error import HTTPError

from .frame_analysis import PROMPT_VERSION, TAGS, has_cjk


class LocalProvider:
    provider = "local"
    prompt_version = PROMPT_VERSION

    def __init__(self, cfg: dict | None = None):
        local = (cfg or {}).get("ai", {}).get("local", {})
        legacy_ollama = str(local.get("ollama_url") or "").rstrip("/")
        if legacy_ollama and not legacy_ollama.endswith("/v1"):
            legacy_ollama += "/v1"
        self.base_url = str(
            local.get("base_url")
            or local.get("lmstudio_url")
            or legacy_ollama
            or "http://127.0.0.1:1234/v1"
        ).rstrip("/")
        self.model = str(local.get("model") or "local-vision")
        ensure_local_model_server(cfg or {}, self.base_url, self.model)

    def analyze_frame(self, frame_path: Path, timestamp: float, video: dict) -> tuple[dict, dict]:
        raw = self._post(self._request_body(frame_path, timestamp, video))
        parsed = _parse(raw)
        if not has_cjk(parsed["summary"] + parsed["suggested_use"]):
            raw = self._post(self._request_body(frame_path, timestamp, video, "你剛剛回英文了。請重新輸出同一個 JSON，但 summary 與 suggested_use 必須全部使用繁體中文。"))
            parsed = _parse(raw)
        return parsed, raw

    def _request_body(self, frame_path: Path, timestamp: float, video: dict, extra: str = "") -> dict:
        image = base64.b64encode(frame_path.read_bytes()).decode("ascii")
        prompt = (
            "請分析這張從影片抽出的單一畫面，供影片剪輯使用。只回傳 strict JSON，不要 Markdown，不要解釋："
            '{"summary": string, "tags": string[], "visual_quality_score": number, '
            '"usefulness_score": number, "suggested_use": string}. '
            "summary 必須用繁體中文描述看得見的畫面內容；suggested_use 必須用繁體中文，例如：短影音、補畫面、產品特寫、轉場、開場。"
            f"tags 必須維持英文，且只能使用這些 tag：{', '.join(TAGS)}。不要猜測畫面中看不到的 tag。"
            "coffee means coffee gear, beans, espresso, pour-over, cup, kettle, dripper, or cafe scene. "
            "matcha means green tea powder, whisk, bowl, or matcha drink. "
            "roasting means beans, roaster, roasting machine, smoke, or roast process. "
            "travel means street, vehicle, walking, city, destination, or trip context. "
            "food means edible food or drinks. landscape means wide outdoor/scenery view. "
            "closeup means subject fills the frame. hands means visible hands. "
            "steam means visible vapor. dripping means visible liquid drip/pour. "
            "usefulness_score 對清楚動作、穩定構圖、特寫、手部、蒸氣、滴落、明確建立場景給高分；模糊、過暗、過曝、不清楚給低分。"
            f"Filename: {video.get('filename')}; timestamp: {timestamp:.1f}s. {extra}"
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
        "suggested_use": str(data.get("suggested_use", "補畫面")),
    }


def _score(value: object) -> float:
    score = float(value or 0)
    if score > 1:
        score = score / 10
    return max(0.0, min(1.0, score))


def ensure_local_model_server(cfg: dict, base_url: str, model: str) -> None:
    if _model_ready(base_url, model):
        return
    lms = shutil.which("lms")
    if not lms:
        raise RuntimeError("找不到 lms CLI，請先在 LM Studio 安裝 CLI")
    subprocess.run([lms, "server", "start"], capture_output=True, text=True, timeout=30)
    if _model_ready(base_url, model, wait_seconds=8):
        return
    local = cfg.get("ai", {}).get("local", {})
    context = str(local.get("context_length") or 8192)
    parallel = str(local.get("parallel") or 1)
    gpu = str(local.get("gpu") or "max")
    ttl = str(local.get("ttl_seconds") or 300)
    subprocess.run([lms, "load", model, "--gpu", gpu, "-c", context, "--parallel", parallel, "--ttl", ttl, "--identifier", model, "-y"], capture_output=True, text=True, timeout=180)
    if not _model_ready(base_url, model, wait_seconds=20):
        raise RuntimeError(f"LM Studio server 已啟動，但模型尚未可用：{model}")


def _model_ready(base_url: str, model: str, wait_seconds: int = 0) -> bool:
    end = time.time() + wait_seconds
    while True:
        try:
            with urllib.request.urlopen(f"{base_url}/models", timeout=2) as res:
                data = json.loads(res.read().decode("utf-8"))
                return any(item.get("id") == model for item in data.get("data", []))
        except Exception:
            if time.time() >= end:
                return False
            time.sleep(1)
