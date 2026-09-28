"""Provider resilience: error classification, retry, circuit breaker, diagnostics.

An incident-response agent that dies because the memory provider is slow is
useless, so every outbound provider call goes through here:

* classify the failure (timeout / DNS / refused / TLS / 401 / 403 / 404 / 429 / 5xx)
* retry with exponential backoff + jitter, only for retryable classes
* trip a circuit breaker so a dead provider costs one fast failure, not N
* hand back a structured diagnosis the UI can render (never raw, never secretful)
"""

from __future__ import annotations

import asyncio
import random
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import urlparse

from recallops.security import scrub_exception

T = TypeVar("T")


class ErrorKind(StrEnum):
    TIMEOUT = "connect_timeout"
    DNS = "dns_failure"
    CONN_REFUSED = "connection_refused"
    TLS = "tls_failure"
    AUTH = "auth_failure"           # 401
    FORBIDDEN = "forbidden"          # 403
    NOT_FOUND = "not_found"          # 404 -> wrong endpoint / bank id
    RATE_LIMIT = "rate_limited"      # 429
    SERVER = "server_error"          # 5xx
    BAD_REQUEST = "bad_request"      # 4xx other
    SCHEMA = "malformed_response"    # unparseable payload
    EMPTY = "empty_response"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown_error"


RETRYABLE: frozenset[ErrorKind] = frozenset(
    {ErrorKind.TIMEOUT, ErrorKind.DNS, ErrorKind.CONN_REFUSED, ErrorKind.TLS, ErrorKind.RATE_LIMIT, ErrorKind.SERVER}
)

HINTS: dict[ErrorKind, tuple[str, ...]] = {
    ErrorKind.TIMEOUT: (
        "The TCP/TLS connection was not established within the timeout window.",
        "Check outbound internet access from the app host (firewall / proxy / corporate egress).",
        "IPv6-first routing: if the endpoint has AAAA records, a broken IPv6 route stalls connects - force IPv4 to test.",
        "Raise the provider timeout gradually (do not remove it) and confirm the region/endpoint.",
    ),
    ErrorKind.DNS: (
        "Hostname could not be resolved.",
        "Verify the configured base URL host spelling and that DNS is reachable from this host.",
        "Container DNS issues usually mean an unreachable resolver - check /etc/resolv.conf.",
    ),
    ErrorKind.CONN_REFUSED: (
        "The host answered but nothing is listening on that port.",
        "The service is down or listening on a different port/interface (common after a container restart).",
    ),
    ErrorKind.TLS: (
        "TLS handshake failed.",
        "Certificate expiry, hostname mismatch, or a proxy terminating TLS unexpectedly.",
        "For local/self-hosted providers prefer http:// unless TLS is properly configured.",
    ),
    ErrorKind.AUTH: (
        "Provider rejected the credentials (401).",
        "Set the correct API key for the configured provider/base URL; keys are not portable across providers.",
    ),
    ErrorKind.FORBIDDEN: (
        "Credentials are valid but not permitted (403).",
        "The key lacks access to this resource/bank; check scopes and the bank id.",
    ),
    ErrorKind.NOT_FOUND: (
        "Endpoint or memory bank not found (404).",
        "Check the base URL path (Hindsight Cloud is a different host from a local server) and the bank id.",
    ),
    ErrorKind.RATE_LIMIT: (
        "Provider rate limit hit (429).",
        "Back off and lower request concurrency; batch retain calls.",
    ),
    ErrorKind.SERVER: ("Provider returned 5xx.", "Upstream provider outage - the system is falling back."),
    ErrorKind.BAD_REQUEST: ("Provider rejected the request payload.", "Validate the request shape against the provider SDK version."),
    ErrorKind.SCHEMA: ("Provider response did not match the expected schema.", "Usually a version mismatch between client and server."),
    ErrorKind.EMPTY: ("Provider returned an empty result.", "Treated as 'no memories' rather than an error."),
    ErrorKind.CANCELLED: ("Request was cancelled.", "Typically a client disconnect or shutdown."),
    ErrorKind.UNKNOWN: ("Unclassified provider failure.", "Check the API's error output; it has been redacted."),
}


class ProviderError(Exception):
    """Structured provider failure. Safe to render in the UI."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = None,
        endpoint: str | None = None,
        attempts: int = 1,
        elapsed_ms: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = ErrorKind(kind)
        self.message = scrub_exception_str(message)
        self.provider = provider
        self.status_code = status_code
        self.endpoint = endpoint
        self.attempts = attempts
        self.elapsed_ms = elapsed_ms
        self.detail = detail or {}

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    @property
    def hints(self) -> list[str]:
        return list(HINTS.get(self.kind, HINTS[ErrorKind.UNKNOWN]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "provider": self.provider,
            "status_code": self.status_code,
            "endpoint": self.endpoint,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
            "retryable": self.retryable,
            "hints": self.hints,
            "detail": self.detail,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.kind}] {self.message}"


def scrub_exception_str(message: str) -> str:
    from recallops.security import redact_text

    return redact_text(message)[:400]


def classify_status(status_code: int) -> ErrorKind:
    if status_code in (401,):
        return ErrorKind.AUTH
    if status_code in (403,):
        return ErrorKind.FORBIDDEN
    if status_code == 404:
        return ErrorKind.NOT_FOUND
    if status_code == 429:
        return ErrorKind.RATE_LIMIT
    if 500 <= status_code < 600:
        return ErrorKind.SERVER
    if 400 <= status_code < 500:
        return ErrorKind.BAD_REQUEST
    return ErrorKind.UNKNOWN


def classify_exception(exc: BaseException) -> ErrorKind:
    """Map any exception (httpx, socket, SDK, schema) onto an ErrorKind."""
    try:
        import httpx
    except Exception:  # pragma: no cover
        httpx = None  # type: ignore[assignment]

    if isinstance(exc, ProviderError):
        return exc.kind
    if httpx is not None:
        if isinstance(exc, httpx.ConnectTimeout):
            return ErrorKind.TIMEOUT
        if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
            return ErrorKind.TIMEOUT
        if isinstance(exc, httpx.ConnectError) and isinstance(exc.__cause__, socket.gaierror):
            return ErrorKind.DNS
        if isinstance(exc, httpx.ConnectError) and isinstance(exc.__cause__, ConnectionRefusedError):
            return ErrorKind.CONN_REFUSED
        if isinstance(exc, httpx.ConnectError) and isinstance(exc.__cause__, ssl.SSLError):
            return ErrorKind.TLS
        if isinstance(exc, httpx.ConnectError):
            return ErrorKind.TIMEOUT if "timed out" in str(exc).lower() else ErrorKind.CONN_REFUSED
        if isinstance(exc, httpx.ProxyError):
            return ErrorKind.CONN_REFUSED
        if isinstance(exc, httpx.TransportError):
            return ErrorKind.TIMEOUT
        if isinstance(exc, httpx.HTTPStatusError):
            return classify_status(exc.response.status_code)
        if isinstance(exc, httpx.RemoteProtocolError):
            return ErrorKind.SCHEMA
    if isinstance(exc, socket.gaierror):
        return ErrorKind.DNS
    if isinstance(exc, socket.timeout | TimeoutError | asyncio.TimeoutError):
        return ErrorKind.TIMEOUT
    if isinstance(exc, ConnectionRefusedError):
        return ErrorKind.CONN_REFUSED
    if isinstance(exc, ssl.SSLError):
        return ErrorKind.TLS
    if isinstance(exc, (KeyError, IndexError, TypeError, ValueError)):
        return ErrorKind.SCHEMA
    if isinstance(exc, asyncio.CancelledError):
        return ErrorKind.CANCELLED
    return ErrorKind.UNKNOWN


# --------------------------------------------------------------------------- retry


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.35
    max_delay: float = 4.0
    jitter: bool = True
    seed: int | None = None

    def delay_for(self, attempt: int) -> float:
        """attempt is 1-based; returns seconds to sleep before the next try."""
        raw = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        if not self.jitter:
            return raw
        rng = random.Random(f"{self.seed}-{attempt}-{time.time_ns()}" if self.seed is None else f"{self.seed}-{attempt}")
        return round(raw * (0.5 + rng.random() * 0.5), 3)

    def timeline(self) -> list[float]:
        return [round(self.base_delay * (2 ** i), 2) for i in range(max(0, self.max_attempts - 1))]


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    provider: str = "",
    endpoint: str | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, ProviderError, float], None] | None = None,
) -> tuple[T, list[dict[str, Any]]]:
    """Call ``fn`` with retry/backoff. Returns (result, attempt_log)."""
    log: list[dict[str, Any]] = []
    last: ProviderError | None = None
    started = time.perf_counter()
    for attempt in range(1, policy.max_attempts + 1):
        try:
            result = await fn()
            log.append({"attempt": attempt, "ok": True, "elapsed_ms": int((time.perf_counter() - started) * 1000)})
            return result, log
        except asyncio.CancelledError:  # pragma: no cover - propagate
            raise
        except BaseException as exc:  # noqa: BLE001 - classify everything
            kind = classify_exception(exc)
            last = (
                exc
                if isinstance(exc, ProviderError)
                else ProviderError(kind, scrub_exception(exc), provider=provider, endpoint=endpoint, attempts=attempt)
            )
            retryable = last.retryable and attempt < policy.max_attempts
            log.append(
                {
                    "attempt": attempt,
                    "ok": False,
                    "kind": last.kind.value,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "retryable": retryable,
                }
            )
            if not retryable:
                last.attempts = attempt
                last.elapsed_ms = int((time.perf_counter() - started) * 1000)
                raise last from exc
            delay = policy.delay_for(attempt)
            if on_retry:
                on_retry(attempt, last, delay)
            await sleep(delay)
    assert last is not None
    raise last


# --------------------------------------------------------------------------- circuit breaker


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    name: str
    threshold: int = 3
    cooldown: float = 20.0
    state: BreakerState = field(default_factory=lambda: BreakerState.CLOSED)
    failures: int = 0
    opened_at: float | None = None
    last_error: str | None = None
    last_kind: str | None = None
    opens: int = 0
    rejections: int = 0
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def _now(self) -> float:
        return self._clock()

    @property
    def is_open(self) -> bool:
        if self.state is BreakerState.OPEN and self.opened_at is not None:
            if self._now() - self.opened_at >= self.cooldown:
                self.state = BreakerState.HALF_OPEN
                return False
            return True
        return self.state is BreakerState.OPEN

    def allow(self) -> bool:
        if self.is_open:
            self.rejections += 1
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.state = BreakerState.CLOSED
        self.opened_at = None
        self.last_error = None
        self.last_kind = None

    def record_failure(self, kind: str, message: str) -> None:
        self.failures += 1
        self.last_kind = kind
        self.last_error = scrub_exception_str(message)
        if self.state is BreakerState.HALF_OPEN or self.failures >= self.threshold:
            if self.state is not BreakerState.OPEN:
                self.opens += 1
            self.state = BreakerState.OPEN
            self.opened_at = self._now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self.failures,
            "threshold": self.threshold,
            "opens": self.opens,
            "rejections": self.rejections,
            "cooldown_s": self.cooldown,
            "last_kind": self.last_kind,
            "last_error": self.last_error,
        }


# --------------------------------------------------------------------------- health + diagnosis


@dataclass
class HealthState(StrEnum):
    CONNECTED = "connected"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


@dataclass
class ProviderHealth:
    name: str
    state: HealthState = field(default_factory=lambda: HealthState.UNKNOWN)
    detail: str = ""
    latency_ms: int | None = None
    endpoint: str | None = None
    kind: ErrorKind | None = None
    hints: list[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "endpoint": self.endpoint,
            "error_kind": self.kind.value if self.kind else None,
            "hints": self.hints,
            "checked_at": self.checked_at.isoformat(),
            **({"extra": self.extra} if self.extra else {}),
        }


def probe_tcp(host: str, port: int, timeout: float = 2.0) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return {
            "ok": False,
            "kind": ErrorKind.DNS.value,
            "detail": f"DNS resolution failed for {host}: {exc.strerror or exc}",
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }
    results: list[dict[str, Any]] = []
    ok = False
    for family, socktype, proto, _canon, sockaddr in infos:
        label = "IPv6" if family == socket.AF_INET6 else "IPv4"
        try:
            with socket.socket(family, socktype, proto) as s:
                s.settimeout(timeout)
                s.connect(sockaddr)
            results.append({"family": label, "address": sockaddr[0], "ok": True})
            ok = True
        except OSError as exc:
            results.append({"family": label, "address": sockaddr[0], "ok": False, "error": type(exc).__name__})
    return {
        "ok": ok,
        "kind": None if ok else ErrorKind.CONN_REFUSED.value,
        "detail": f"TCP connect to {host}:{port} {'succeeded' if ok else 'failed'}",
        "attempts": results,
        "latency_ms": int((time.perf_counter() - start) * 1000),
    }


async def diagnose_endpoint(
    base_url: str,
    *,
    probe_timeout: float = 4.0,
    ready_paths: tuple[str, ...] = ("/health/ready", "/health", "/v1/health"),
) -> dict[str, Any]:
    """Full connectivity diagnosis for a provider URL. Never raises."""
    parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    report: dict[str, Any] = {
        "endpoint": f"{parsed.scheme}://{parsed.netloc}",
        "host": host,
        "port": port,
        "scheme": parsed.scheme,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "steps": [],
    }
    report["steps"].append({"step": "dns", **probe_tcp(host, port, timeout=min(probe_timeout, 2.0))})
    if not report["steps"][-1]["ok"]:
        report["ok"] = False
        report["verdict"] = "DNS resolution failed - the host name could not be resolved."
        return report
    report["tls"] = None
    if parsed.scheme == "https":
        try:
            ctx = ssl.create_default_context()
            async with asyncio.to_thread(_tls_handshake, host, port, ctx, probe_timeout):
                report["tls"] = {"ok": True, "detail": "TLS handshake succeeded"}
        except Exception as exc:  # noqa: BLE001
            report["tls"] = {"ok": False, "kind": ErrorKind.TLS.value, "detail": scrub_exception(exc)}
    try:
        import httpx

        async with httpx.AsyncClient(timeout=probe_timeout) as client:
            for path in ready_paths:
                started = time.perf_counter()
                try:
                    resp = await client.get(f"{base_url.rstrip('/')}{path}")
                    entry = {
                        "step": "readiness",
                        "path": path,
                        "status": resp.status_code,
                        "ok": resp.status_code < 500,
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                    }
                    report["steps"].append(entry)
                    if resp.status_code < 500:
                        report["ok"] = True
                        report["verdict"] = f"Provider reachable ({path} -> {resp.status_code})."
                        report["ready_path"] = path
                        return report
                except Exception as exc:  # noqa: BLE001
                    report["steps"].append(
                        {
                            "step": "readiness",
                            "path": path,
                            "ok": False,
                            "kind": classify_exception(exc).value,
                            "detail": scrub_exception(exc),
                            "latency_ms": int((time.perf_counter() - started) * 1000),
                        }
                    )
    except Exception as exc:  # noqa: BLE001 pragma: no cover
        report["steps"].append({"step": "readiness", "ok": False, "detail": scrub_exception(exc)})
    report["ok"] = any(s.get("ok") for s in report["steps"] if s.get("step") == "readiness")
    report["verdict"] = "TCP reachable but no readiness endpoint answered." if report["ok"] else "Endpoint not reachable."
    return report


def _tls_handshake(host: str, port: int, ctx: ssl.SSLContext, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            _ = ssock.getpeercert()


__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "ErrorKind",
    "HealthState",
    "ProviderError",
    "ProviderHealth",
    "RetryPolicy",
    "classify_exception",
    "classify_status",
    "diagnose_endpoint",
    "probe_tcp",
    "retry_async",
]
