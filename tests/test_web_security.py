import ipaddress

import pytest

from video_vault.web_security import WebSecurityError, parse_single_range, validate_host_header, validate_local_bind_host, validate_origin_headers


def test_non_loopback_bind_is_rejected():
    with pytest.raises(WebSecurityError, match="local-only"):
        validate_local_bind_host("0.0.0.0")
    with pytest.raises(WebSecurityError):
        validate_local_bind_host("192.168.1.20")


def test_range_parser_supports_open_ended_and_suffix_ranges():
    assert parse_single_range("bytes=10-19", 100) == (10, 19)
    assert parse_single_range("bytes=10-", 100) == (10, 99)
    assert parse_single_range("bytes=-10", 100) == (90, 99)


def test_range_parser_rejects_multi_range_and_unsatisfiable():
    with pytest.raises(WebSecurityError, match="單一"):
        parse_single_range("bytes=0-1,4-5", 10)
    with pytest.raises(WebSecurityError, match="超出"):
        parse_single_range("bytes=20-30", 10)


def test_host_allowlist_rejects_dns_rebinding_and_wrong_port():
    validate_host_header("127.0.0.1:8765", "127.0.0.1", 8765)
    validate_host_header("[::1]:8765", "::1", 8765)
    with pytest.raises(WebSecurityError):
        validate_host_header("evil.example:8765", "127.0.0.1", 8765)
    with pytest.raises(WebSecurityError):
        validate_host_header("127.0.0.1:9999", "127.0.0.1", 8765)


def test_mutation_requires_same_origin_and_csrf():
    headers = {"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765"}
    validate_origin_headers(headers, "127.0.0.1", 8765, csrf_token="token", supplied_token="token")
    with pytest.raises(WebSecurityError, match="CSRF"):
        validate_origin_headers(headers, "127.0.0.1", 8765, csrf_token="token", supplied_token="wrong")
    with pytest.raises(WebSecurityError, match="Origin"):
        validate_origin_headers({**headers, "origin": "http://evil.example:8765"}, "127.0.0.1", 8765, csrf_token="token", supplied_token="token")
    with pytest.raises(WebSecurityError, match="Origin/Referer"):
        validate_origin_headers({"host": "127.0.0.1:8765"}, "127.0.0.1", 8765, csrf_token="token", supplied_token="token")
