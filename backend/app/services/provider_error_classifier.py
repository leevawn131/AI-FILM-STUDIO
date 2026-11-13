"""
Shared structured error classification and response parsing helpers for Google Cloud AI adapters.

Avoids brittle string-only checks by inspecting structured HTTP status codes, Google SDK exception
attributes (`GenAiError`, `APIError`), httpx/urllib responses, and standard network exceptions.
"""
from dataclasses import dataclass
from typing import Any, Literal, Optional
import asyncio
import base64
import json
import socket
import urllib.error


ProviderErrorCategory = Literal["rejected_before_generation", "provider_outcome_unknown"]

# Confirmed pre-generation / client-side rejection HTTP codes:
# Safe to conclude Google rejected the request prior to running billing/generation.
PRE_GENERATION_REJECTION_HTTP_CODES = frozenset({400, 401, 403, 404, 422, 429})

# Confirmed Google RPC / API status names indicating client validation/auth rejection:
PRE_GENERATION_REJECTION_STATUS_NAMES = frozenset({
    "INVALID_ARGUMENT",
    "FAILED_PRECONDITION",
    "PERMISSION_DENIED",
    "UNAUTHENTICATED",
    "NOT_FOUND",
    "RESOURCE_EXHAUSTED",
    "UNIMPLEMENTED",
    "OUT_OF_RANGE",
})

# Transient, timeout, or server-side failure status codes/names where remote work may have started:
OUTCOME_UNKNOWN_HTTP_CODES = frozenset({408, 425, 499, 500, 502, 503, 504})
OUTCOME_UNKNOWN_STATUS_NAMES = frozenset({
    "DEADLINE_EXCEEDED",
    "UNAVAILABLE",
    "INTERNAL",
    "ABORTED",
    "UNKNOWN",
    "CANCELLED",
    "DATA_LOSS",
})


class SubmissionResponseAnomalyError(RuntimeError):
    """Raised when a remote dispatch call returns a malformed/empty payload without an operation ID."""


@dataclass(frozen=True)
class ClassifiedProviderError:
    category: ProviderErrorCategory
    http_status: Optional[int]
    provider_status: Optional[str]
    reason: str

    @property
    def is_confirmed_pre_generation_rejection(self) -> bool:
        return self.category == "rejected_before_generation"

    @property
    def is_outcome_unknown(self) -> bool:
        return self.category == "provider_outcome_unknown"


def _real_int(val: Any) -> Optional[int]:
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    return None


def _real_str(val: Any) -> Optional[str]:
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def safe_str(val: Any) -> Optional[str]:
    """Returns stripped string only when `val` is an actual `str` (defeats MagicMock truthiness)."""
    return _real_str(val)


def decode_media_bytes(raw_data: Any) -> Optional[bytes]:
    """
    Safely decodes bytes or base64 strings into raw `bytes`.
    Returns `None` for non-byte/non-string types (including MagicMock objects).
    """
    if isinstance(raw_data, (bytes, bytearray)):
        data = bytes(raw_data)
        return data if data else None
    if isinstance(raw_data, str):
        payload = raw_data.strip()
        if not payload:
            return None
        if payload.startswith("data:") and "," in payload:
            payload = payload.split(",", 1)[1]
        try:
            decoded = base64.b64decode(payload, validate=False)
            return decoded if decoded else None
        except Exception:
            return None
    return None


def _extract_structured_status(exc: BaseException) -> tuple[Optional[int], Optional[str]]:
    """
    Walks an exception and its cause chain to extract real HTTP status code and provider status name.
    Strictly ignores MagicMock / dynamic non-primitive attributes.
    """
    visited: set[int] = set()
    queue: list[BaseException] = [exc]
    found_http: Optional[int] = None
    found_status: Optional[str] = None

    while queue:
        cur = queue.pop(0)
        if id(cur) in visited:
            continue
        visited.add(id(cur))

        # 1. Direct status attributes (`GenAiError.status_code`, `APIError.code`, `HTTPError.code`)
        for attr in ("status_code", "code"):
            code = _real_int(getattr(cur, attr, None))
            if code is not None and 100 <= code <= 599 and found_http is None:
                found_http = code

        status_str = _real_str(getattr(cur, "status", None))
        if status_str and found_status is None:
            found_status = status_str.upper()

        # 2. Wrapped HTTP response object (`httpx.HTTPStatusError.response`, `APIError.response`)
        resp = getattr(cur, "response", None)
        if resp is not None:
            resp_code = _real_int(getattr(resp, "status_code", None))
            if resp_code is not None and 100 <= resp_code <= 599 and found_http is None:
                found_http = resp_code

        # 3. `GenAiError.data.error` structure (`google.genai._gaos`)
        data = getattr(cur, "data", None)
        if data is not None:
            err_obj = getattr(data, "error", None)
            if err_obj is not None:
                err_code = _real_int(getattr(err_obj, "code", None))
                if err_code is not None and 100 <= err_code <= 599 and found_http is None:
                    found_http = err_code
                err_status = _real_str(getattr(err_obj, "status", None))
                if err_status and found_status is None:
                    found_status = err_status.upper()

        # 4. Structured JSON body if present on GenAiError.body
        body = _real_str(getattr(cur, "body", None))
        if body and body.startswith("{"):
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    err_dict = parsed.get("error") if isinstance(parsed.get("error"), dict) else parsed
                    b_code = _real_int(err_dict.get("code"))
                    if b_code is not None and 100 <= b_code <= 599 and found_http is None:
                        found_http = b_code
                    b_status = _real_str(err_dict.get("status"))
                    if b_status and found_status is None:
                        found_status = b_status.upper()
            except Exception:
                pass

        for linked in (getattr(cur, "__cause__", None), getattr(cur, "__context__", None)):
            if isinstance(linked, BaseException) and id(linked) not in visited:
                queue.append(linked)

    return found_http, found_status


def classify_provider_exception(exc: BaseException) -> ClassifiedProviderError:
    """
    Classifies a provider exception into either:
    - `rejected_before_generation`: confirmed 4xx pre-generation client/auth/validation rejection
    - `provider_outcome_unknown`: timeout, network failure, 5xx, missing operation ID, or ambiguous failure

    Default posture on ambiguity is ALWAYS `provider_outcome_unknown` so local billing never drops
    accountability when Google may have started or completed billable work.
    """
    if isinstance(exc, SubmissionResponseAnomalyError):
        return ClassifiedProviderError(
            category="provider_outcome_unknown",
            http_status=None,
            provider_status="ANOMALY_MISSING_OPERATION_ID",
            reason=str(exc) or "Provider response lacked operation ID",
        )

    http_status, provider_status = _extract_structured_status(exc)
    msg = str(exc)
    msg_lower = msg.lower()

    # Check timeout/network exceptions explicitly before heuristics
    network_or_timeout_types: tuple[type[BaseException], ...] = (
        TimeoutError,
        asyncio.TimeoutError,
        socket.timeout,
        ConnectionError,
        urllib.error.URLError,
    )
    # Note: urllib.error.HTTPError is a subclass of URLError, so check structured HTTP status first!
    if http_status is not None:
        if http_status in PRE_GENERATION_REJECTION_HTTP_CODES:
            return ClassifiedProviderError(
                category="rejected_before_generation",
                http_status=http_status,
                provider_status=provider_status,
                reason=f"Confirmed provider client rejection (HTTP {http_status}): {msg[:240]}",
            )
        if http_status in OUTCOME_UNKNOWN_HTTP_CODES or http_status >= 500:
            return ClassifiedProviderError(
                category="provider_outcome_unknown",
                http_status=http_status,
                provider_status=provider_status,
                reason=f"Ambiguous/server/timeout provider response (HTTP {http_status}): {msg[:240]}",
            )

    if provider_status is not None:
        if provider_status in PRE_GENERATION_REJECTION_STATUS_NAMES:
            return ClassifiedProviderError(
                category="rejected_before_generation",
                http_status=http_status,
                provider_status=provider_status,
                reason=f"Confirmed provider status rejection ({provider_status}): {msg[:240]}",
            )
        if provider_status in OUTCOME_UNKNOWN_STATUS_NAMES:
            return ClassifiedProviderError(
                category="provider_outcome_unknown",
                http_status=http_status,
                provider_status=provider_status,
                reason=f"Ambiguous/transient provider status ({provider_status}): {msg[:240]}",
            )

    # Local ValueError raised during pre-dispatch parameter validation counts as rejected before dispatch
    if isinstance(exc, ValueError):
        return ClassifiedProviderError(
            category="rejected_before_generation",
            http_status=400,
            provider_status="INVALID_ARGUMENT",
            reason=f"Pre-dispatch validation error: {msg[:240]}",
        )

    # Non-HTTPError network/timeout exceptions always map to outcome unknown
    if isinstance(exc, network_or_timeout_types) and not isinstance(exc, urllib.error.HTTPError):
        return ClassifiedProviderError(
            category="provider_outcome_unknown",
            http_status=http_status,
            provider_status=provider_status or type(exc).__name__,
            reason=f"Network/timeout uncertainty ({type(exc).__name__}): {msg[:240]}",
        )

    # Check httpx / requests timeout class names without requiring hard runtime dependency
    exc_class_name = type(exc).__name__
    if any(token in exc_class_name for token in ("Timeout", "ConnectError", "NetworkError", "RemoteProtocolError")):
        return ClassifiedProviderError(
            category="provider_outcome_unknown",
            http_status=http_status,
            provider_status=exc_class_name,
            reason=f"HTTP client transport uncertainty ({exc_class_name}): {msg[:240]}",
        )

    # Last-resort structured string tokens only when neither status code nor typed exception resolved
    if any(tok in msg_lower for tok in ("timed out", "timeout", "connection reset", "broken pipe", "unavailable")):
        return ClassifiedProviderError(
            category="provider_outcome_unknown",
            http_status=http_status,
            provider_status=provider_status,
            reason=f"Transport/timeout indicator: {msg[:240]}",
        )

    # Check Google client/validation messages ONLY when no ambiguity indicator is present
    if any(
        tok in msg_lower
        for tok in (
            "invalid_argument",
            "permission_denied",
            "unauthenticated",
            "invalid api key",
            "api key not valid",
            "not found (404)",
            "unsupported model",
            "deprecated",
            "bad request",
            "400 ",
            "401 ",
            "403 ",
            "404 ",
            "422 ",
            "429 ",
        )
    ):
        return ClassifiedProviderError(
            category="rejected_before_generation",
            http_status=http_status,
            provider_status=provider_status,
            reason=f"Provider client/validation error: {msg[:240]}",
        )

    # Safe default: never accidentally forgive charges on unknown failures
    return ClassifiedProviderError(
        category="provider_outcome_unknown",
        http_status=http_status,
        provider_status=provider_status or exc_class_name,
        reason=f"Unclassified provider error treated as outcome unknown: {msg[:240]}",
    )
