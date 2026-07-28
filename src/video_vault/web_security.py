"""Local WebUI security policy and bounded media range parsing."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class WebSecurityError(ValueError):
    def __init__(self, code: str, message: str, *, action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = "security"
        self.retryable = False
        self.action = action

    def as_dict(self) -> dict[str, object]:
        return {"ok": False, "code": self.code, "error": self.message, "category": self.category, "retryable": self.retryable, "details": {}, "action": self.action}


def validate_local_bind_host(host: str) -> str:
    value = str(host or "").strip()
    try:
        addresses = {ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(value, None, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise WebSecurityError("non_loopback_bind", "本版本只支援 local-only；主機名稱無法確認為 loopback", action="請使用 127.0.0.1 或 ::1") from exc
    if not addresses or not all(address.is_loopback for address in addresses):
        raise WebSecurityError("non_loopback_bind", "本版本只支援 local-only；未驗證 LAN mode 不安全且不支援", action="請使用 127.0.0.1 或 ::1")
    return value


def _authority(value: str) -> tuple[str, int | None]:
    parsed = urlsplit("//" + str(value or ""))
    if not parsed.hostname:
        return "", None
    try:
        return parsed.hostname.lower(), parsed.port
    except ValueError:
        return "", None


def validate_host_header(host_header: str | None, bind_host: str, bind_port: int) -> None:
    host, port = _authority(str(host_header or ""))
    allowed_host = "localhost" if bind_host.lower() == "localhost" else bind_host.lower().strip("[]")
    allowed = {allowed_host, "127.0.0.1", "::1", "localhost"}
    if host not in allowed or (port is not None and int(port) != int(bind_port)):
        raise WebSecurityError("invalid_host", "拒絕未核准的 Host header", action="請從本機 UI 位址重新開啟")


def validate_origin_headers(headers, bind_host: str, bind_port: int, *, csrf_token: str | None, supplied_token: str | None) -> None:
    validate_host_header(headers.get("host"), bind_host, bind_port)
    origin = headers.get("origin")
    referer = headers.get("referer")
    target_hosts = {"localhost", "127.0.0.1", "[::1]", "::1", bind_host.lower()}
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname is None or parsed.hostname.lower() not in target_hosts or (parsed.port is not None and parsed.port != bind_port):
            raise WebSecurityError("invalid_origin", "Origin 與本機 UI 不一致，已拒絕變更操作", action="請從同一個本機 UI 分頁操作")
    elif referer:
        parsed = urlsplit(referer)
        if parsed.scheme != "http" or parsed.hostname is None or parsed.hostname.lower() not in target_hosts or (parsed.port is not None and parsed.port != bind_port):
            raise WebSecurityError("invalid_referer", "Referer 與本機 UI 不一致，已拒絕變更操作", action="請從同一個本機 UI 分頁操作")
    else:
        raise WebSecurityError("missing_origin", "缺少 Origin/Referer，已拒絕瀏覽器變更操作", action="請重新從本機 UI 送出")
    if not csrf_token or supplied_token != csrf_token:
        raise WebSecurityError("csrf_failed", "CSRF token 無效或缺失，已拒絕變更操作", action="重新整理頁面後再試")


def parse_single_range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise WebSecurityError("invalid_range", "只支援單一 byte range", action="重新載入影片預覽")
    spec = value[6:].strip()
    if "-" not in spec:
        raise WebSecurityError("invalid_range", "Range 格式無效", action="重新載入影片預覽")
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError
            start, end = max(0, size - suffix), size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
            if start < 0 or end < start or start >= size:
                raise ValueError
            end = min(end, size - 1)
    except (TypeError, ValueError) as exc:
        raise WebSecurityError("invalid_range", "Range 超出檔案範圍", action="重新載入影片預覽") from exc
    return start, end


def parse_content_length(
    value: str | None,
    *,
    maximum: int,
    required: bool = False,
) -> int:
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise WebSecurityError(
                "content_length_required",
                "缺少 Content-Length，已拒絕請求",
                action="請重新送出請求",
            )
        return 0
    try:
        length = int(raw)
    except (TypeError, ValueError) as exc:
        raise WebSecurityError(
            "invalid_content_length",
            "Content-Length 無效",
            action="請重新送出請求",
        ) from exc
    if length < 0:
        raise WebSecurityError(
            "invalid_content_length",
            "Content-Length 不可為負數",
            action="請重新送出請求",
        )
    if length > int(maximum):
        raise WebSecurityError(
            "request_too_large",
            "請求內容超過本機服務允許的大小",
            action="請縮小請求或調整本機上傳限制",
        )
    return length


__all__ = [
    "WebSecurityError",
    "parse_content_length",
    "parse_single_range",
    "validate_host_header",
    "validate_local_bind_host",
    "validate_origin_headers",
]
