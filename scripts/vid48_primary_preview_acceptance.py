"""VID-48 isolated primary Visual Style Preview acceptance."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import socket
import sys
import time
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video_vault.config import load_config
from video_vault.ui import run_ui


def _serve(cfg: dict, port: int) -> None:
    run_ui(cfg, "127.0.0.1", port)


def _wait_for_port(port: int) -> None:
    deadline = time.perf_counter() + 30
    while time.perf_counter() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"UI did not listen on {port}")


def _json_request(url: str, *, method: str = "GET", body: dict | None = None, headers: dict[str, str] | None = None) -> dict:
    request = Request(url, method=method, headers=headers or {})
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=360) as response:
        return json.loads(response.read().decode("utf-8"))


def _summarize(payload: dict, label: str, elapsed_ms: int) -> dict:
    variants = payload.get("variants") or []
    return {
        "label": label,
        "elapsed_ms": elapsed_ms,
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "preview_scope": payload.get("preview_scope"),
        "preview_scope_version": payload.get("preview_scope_version"),
        "variant_count": len(variants),
        "styles": sorted({str((item.get("visual_style") or {}).get("visual_style_id") or "") for item in variants}),
        "variants": [
            {
                "style": (item.get("visual_style") or {}).get("visual_style_id"),
                "role": item.get("title_role"),
                "kind": item.get("preview_kind"),
                "cache_hit": item.get("cache_hit"),
                "file": item.get("file"),
            }
            for item in variants
        ],
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--port", type=int, default=18748)
    args = parser.parse_args()
    cfg = load_config(args.config)
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_serve, args=(cfg, args.port), daemon=True)
    process.start()
    try:
        _wait_for_port(args.port)
        base = f"http://127.0.0.1:{args.port}"
        security = _json_request(f"{base}/api/security")
        headers = {
            "Host": f"127.0.0.1:{args.port}",
            "Origin": base,
            "x-video-vault-csrf": str(security.get("csrf_token") or ""),
        }
        body = {"project_id": 1, "force": False, "overrides": {}, "scope": "primary"}
        runs = []
        for label in ("cold_or_current", "same_contract_retry"):
            started = time.perf_counter()
            payload = _json_request(f"{base}/api/project/visual-style/preview", method="POST", body=body, headers=headers)
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            runs.append(_summarize(payload, label, elapsed_ms))
        report = {
            "acceptance": "VID-48 primary Visual Style Preview",
            "project_id": 1,
            "scope": "primary",
            "primary_target_seconds": 90,
            "runs": runs,
            "server_pid": process.pid,
        }
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=15)


if __name__ == "__main__":
    raise SystemExit(main())
