"""Auditable runtime capability records shared by Doctor and Perception.

The registry is runtime evidence under the configured library root.  It never
edits configuration and a record is usable only when its provider, model,
validated loopback endpoint, and perception contract binding match exactly.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import tempfile
from typing import Any, Mapping
from urllib.parse import urlparse


CAPABILITY_REGISTRY_SCHEMA_VERSION = 1
CAPABILITY_REGISTRY_CONTRACT = "runtime-capability-registry-v1"
CAPABILITY_NETWORK_SCOPE_POLICY = "loopback_only"


def validate_local_endpoint_scope(base_url: str) -> dict[str, Any]:
    """Validate and pin a configured provider endpoint to loopback only."""

    configured = str(base_url).rstrip("/")
    evidence: dict[str, Any] = {
        "network_scope_policy": CAPABILITY_NETWORK_SCOPE_POLICY,
        "configured_endpoint": configured,
        "validated_network_scope": "blocked",
        "resolved_addresses": [],
        "dns_validation": "not_attempted",
    }
    parsed = urlparse(configured)
    try:
        port = parsed.port
    except ValueError:
        port = None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return {**evidence, "status": "blocked", "error_code": "invalid_local_endpoint"}

    resolved: list[str] = []
    if host == "localhost":
        try:
            infos = socket.getaddrinfo(host, port or 80, type=socket.SOCK_STREAM)
            for info in infos:
                address = str(info[4][0]).split("%", 1)[0]
                if address not in resolved:
                    resolved.append(address)
            evidence["dns_validation"] = "all_addresses_checked"
        except (OSError, socket.gaierror):
            return {
                **evidence,
                "status": "blocked",
                "error_code": "loopback_hostname_unresolved",
                "dns_validation": "failed",
            }
    else:
        try:
            parsed_ip = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            return {
                **evidence,
                "status": "blocked",
                "error_code": "host_not_loopback_allowlist",
                "dns_validation": "not_applicable",
            }
        resolved = [str(parsed_ip)]
        evidence["dns_validation"] = "ip_literal"

    resolved = sorted(set(resolved))
    evidence["resolved_addresses"] = resolved
    try:
        addresses = [ipaddress.ip_address(address) for address in resolved]
    except ValueError:
        return {**evidence, "status": "blocked", "error_code": "dns_address_invalid"}
    if not addresses or not all(address.is_loopback for address in addresses):
        return {**evidence, "status": "blocked", "error_code": "dns_resolved_non_loopback"}

    selected = next((address for address in addresses if address.version == 4), addresses[0])
    selected_host = str(selected)
    netloc = f"[{selected_host}]" if selected.version == 6 else selected_host
    if port is not None:
        netloc = f"{netloc}:{port}"
    validated_endpoint = parsed._replace(netloc=netloc).geturl().rstrip("/")
    return {
        **evidence,
        "status": "pass",
        "validated_network_scope": "loopback",
        "validated_endpoint": validated_endpoint,
        "selected_loopback_address": selected_host,
    }


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _endpoint_identity(scope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "configured_endpoint": str(scope.get("configured_endpoint") or "").rstrip("/"),
        "validated_endpoint": str(scope.get("validated_endpoint") or "").rstrip("/"),
        "validated_network_scope": str(scope.get("validated_network_scope") or ""),
        "resolved_addresses": sorted(str(item) for item in scope.get("resolved_addresses") or []),
        "network_scope_policy": str(scope.get("network_scope_policy") or ""),
    }


def capability_binding(
    *,
    provider: str,
    model: str,
    endpoint_scope: Mapping[str, Any],
    provider_contract_version: str,
    prompt_contract_version: str,
    capability_schema_version: int,
) -> dict[str, Any]:
    endpoint = _endpoint_identity(endpoint_scope)
    endpoint["identity_fingerprint"] = _canonical_hash(endpoint)
    binding = {
        "registry_contract": CAPABILITY_REGISTRY_CONTRACT,
        "registry_schema_version": CAPABILITY_REGISTRY_SCHEMA_VERSION,
        "provider": str(provider),
        "model": str(model),
        "endpoint_identity": endpoint,
        "provider_contract_version": str(provider_contract_version),
        "prompt_contract_version": str(prompt_contract_version),
        "capability_schema_version": int(capability_schema_version),
    }
    binding["binding_fingerprint"] = _canonical_hash(binding)
    return binding


def capability_record_path(cfg: Mapping[str, Any], binding_fingerprint: str) -> Path:
    return (
        Path(str(cfg.get("library_root") or ""))
        / "05_index"
        / "runtime_capabilities"
        / "multi_image"
        / f"{binding_fingerprint}.json"
    )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


def persist_probe_capability(
    cfg: Mapping[str, Any],
    *,
    provider: str,
    model: str,
    endpoint_scope: Mapping[str, Any],
    verified: bool,
    maximum_images: int,
    supported_image_formats: list[str] | tuple[str, ...],
    provider_contract_version: str,
    prompt_contract_version: str,
    capability_schema_version: int,
    probe_evidence: Mapping[str, Any] | None = None,
    verified_at: str | None = None,
) -> dict[str, Any]:
    """Atomically persist a positive record or a same-binding failure tombstone."""

    if endpoint_scope.get("status") != "pass":
        return {"status": "blocked", "reason": "endpoint_scope_not_verified"}
    if not str(cfg.get("library_root") or "").strip():
        return {"status": "blocked", "reason": "library_root_missing"}
    binding = capability_binding(
        provider=provider,
        model=model,
        endpoint_scope=endpoint_scope,
        provider_contract_version=provider_contract_version,
        prompt_contract_version=prompt_contract_version,
        capability_schema_version=capability_schema_version,
    )
    timestamp = verified_at or datetime.now(timezone.utc).isoformat()
    formats = sorted({str(item).lower() for item in supported_image_formats if str(item).strip()})
    record: dict[str, Any] = {
        "registry_contract": CAPABILITY_REGISTRY_CONTRACT,
        "registry_schema_version": CAPABILITY_REGISTRY_SCHEMA_VERSION,
        "binding": binding,
        "verification_source": "verified_probe" if verified else "probe_failed",
        "verified_at": timestamp,
        "multi_image_verified": bool(verified),
        "maximum_images": int(maximum_images) if verified else 0,
        "supported_image_formats": formats if verified else [],
        "probe": {
            "status": str((probe_evidence or {}).get("status") or ""),
            "semantic_validation": str((probe_evidence or {}).get("semantic_validation") or ""),
            "image_count": int((probe_evidence or {}).get("image_count") or 0),
            "validated_network_scope": str((probe_evidence or {}).get("validated_network_scope") or ""),
            "request_reasoning_control": str((probe_evidence or {}).get("request_reasoning_control") or ""),
        },
    }
    fingerprint_payload = {key: value for key, value in record.items() if key != "verification_fingerprint"}
    record["verification_fingerprint"] = _canonical_hash(fingerprint_payload)
    path = capability_record_path(cfg, str(binding["binding_fingerprint"]))
    try:
        _atomic_write_json(path, record)
    except OSError as exc:
        return {
            "status": "blocked",
            "reason": "registry_write_failed",
            "error": type(exc).__name__,
            "binding_fingerprint": binding["binding_fingerprint"],
        }
    return {
        "status": "persisted",
        "record_path": str(path),
        "binding_fingerprint": binding["binding_fingerprint"],
        "verification_fingerprint": record["verification_fingerprint"],
        "verification_source": record["verification_source"],
        "verified_at": timestamp,
    }


def resolve_verified_probe_capability(
    cfg: Mapping[str, Any],
    *,
    provider: str,
    model: str,
    base_url: str,
    provider_contract_version: str,
    prompt_contract_version: str,
    capability_schema_version: int,
) -> dict[str, Any]:
    """Resolve one exact verified binding; all missing/mismatched state fails closed."""

    scope = validate_local_endpoint_scope(base_url)
    if scope.get("status") != "pass":
        return {"status": "blocked", "reason": "endpoint_scope_not_verified", "endpoint_scope": scope}
    binding = capability_binding(
        provider=provider,
        model=model,
        endpoint_scope=scope,
        provider_contract_version=provider_contract_version,
        prompt_contract_version=prompt_contract_version,
        capability_schema_version=capability_schema_version,
    )
    path = capability_record_path(cfg, str(binding["binding_fingerprint"]))
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "status": "missing",
            "reason": "verified_probe_record_missing",
            "binding_fingerprint": binding["binding_fingerprint"],
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "status": "blocked",
            "reason": "verified_probe_record_unreadable",
            "binding_fingerprint": binding["binding_fingerprint"],
        }
    if not isinstance(record, Mapping):
        return {"status": "blocked", "reason": "verified_probe_record_not_object"}
    fingerprint_payload = {key: value for key, value in record.items() if key != "verification_fingerprint"}
    expected_record_fingerprint = _canonical_hash(fingerprint_payload)
    integrity_checks = {
        "registry_contract": record.get("registry_contract") == CAPABILITY_REGISTRY_CONTRACT,
        "registry_schema_version": record.get("registry_schema_version") == CAPABILITY_REGISTRY_SCHEMA_VERSION,
        "binding": record.get("binding") == binding,
        "verification_fingerprint": record.get("verification_fingerprint") == expected_record_fingerprint,
        "verified_at": bool(str(record.get("verified_at") or "")),
    }
    if not all(integrity_checks.values()):
        return {
            "status": "blocked",
            "reason": "verified_probe_record_invalid",
            "checks": integrity_checks,
            "binding_fingerprint": binding["binding_fingerprint"],
        }
    if record.get("verification_source") == "probe_failed" and record.get("multi_image_verified") is False:
        return {
            "status": "blocked",
            "reason": "verified_probe_failed",
            "probe": dict(record.get("probe") or {}),
            "binding_fingerprint": binding["binding_fingerprint"],
            "verification_fingerprint": str(record.get("verification_fingerprint") or ""),
        }
    capability_checks = {
        "verification_source": record.get("verification_source") == "verified_probe",
        "multi_image_verified": record.get("multi_image_verified") is True,
        "maximum_images": isinstance(record.get("maximum_images"), int) and int(record.get("maximum_images") or 0) >= 3,
        "supported_image_formats": "jpeg" in [str(item).lower() for item in record.get("supported_image_formats") or []],
    }
    if not all(capability_checks.values()):
        return {
            "status": "blocked",
            "reason": "verified_probe_record_invalid",
            "checks": {**integrity_checks, **capability_checks},
            "binding_fingerprint": binding["binding_fingerprint"],
        }
    return {
        "status": "pass",
        "capability": {
            "supports_multi_image": True,
            "maximum_images": int(record["maximum_images"]),
            "supported_image_formats": sorted(str(item).lower() for item in record["supported_image_formats"]),
            "provider_contract_version": str(binding["provider_contract_version"]),
            "prompt_contract_version": str(binding["prompt_contract_version"]),
            "schema_version": int(binding["capability_schema_version"]),
            "capability_source": "verified_probe",
            "verification_source": "verified_probe",
            "verified_at": str(record["verified_at"]),
            "binding_fingerprint": str(binding["binding_fingerprint"]),
            "verification_fingerprint": str(record["verification_fingerprint"]),
            "endpoint_identity": dict(binding["endpoint_identity"]),
            "registry_contract": CAPABILITY_REGISTRY_CONTRACT,
        },
        "record_path": str(path),
    }
