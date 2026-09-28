"""Security helpers: secret redaction and untrusted-input hardening.

Two jobs, both required for an incident-response product:

1. Incident logs, alerts and tool output are pasted in from the wild. They may
   contain credentials. Nothing secret may reach an LLM provider, the Hindsight
   memory layer, the database, or the browser.
2. Those same strings are *untrusted input*. A log line that says "ignore your
   previous instructions" must never be able to steer the agent.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Credential-ish key names. Word-ish boundaries so "api_keys_total" (a metric)
# is not redacted while "api_key=sk-..." is.
_SECRET_KEY_RE = re.compile(
    r"(?i)\b("
    r"api[_-]?key|apikey|secret[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token"
    r"|bearer|authorization|password|passwd|pwd|private[_-]?key|session[_-]?token"
    r"|connection[_-]?string|dsn|refresh[_-]?token"
    r")\b(\s*[:=]\s*|\"\s*:\s*\")(\"?)([^\s\"',;)\]}]{3,})[\"']?"
)

# Well-known literal token shapes (redacted even without a key name).
_TOKEN_SHAPES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), f"sk-{REDACTED}"),
    (re.compile(r"\bhst_[A-Za-z0-9]{12,}"), f"hst_{REDACTED}"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), f"github_{REDACTED}"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"), f"jwt_{REDACTED}"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), f"email_{REDACTED}"),
    (re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s:@/]+:[^\s@/]+@[^\s]+"), f"{REDACTED}://{REDACTED}"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"), f"Bearer {REDACTED}"),
)

# Prompt-injection markers inside untrusted text.
_INJECTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)"), "[neutralized:instruction-override]"),
    (re.compile(r"(?i)\byou\s+are\s+now\s+(a|an|the)\b.{0,40}"), "[neutralized:role-override]"),
    (re.compile(r"(?i)\b(system|developer|assistant)\s*:\s*"), "[neutralized:role-prefix]"),
    (re.compile(r"(?i)<\s*/?\s*(system|instructions?|s)\s*>"), "[neutralized:xml-tag]"),
    (re.compile(r"(?i)\b(reveal|print|output|show)\s+(me\s+)?(your|the)\s+(api[_-]?key|secret|token|password|system prompt|credentials?)"), "[neutralized:exfiltration-attempt]"),
    (re.compile(r"(?i)\b(exfiltrate|curl\s+https?://|wget\s+https?://)"), "[neutralized:exfiltration-attempt]"),
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UNSAFE_WS = re.compile(r"[ \t]{3,}")
_MAX_FIELD_LEN = 2000
_MAX_LOG_LINE = 600


def redact_text(value: Any) -> str:
    """Remove credential-looking substrings from a single string."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = _SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}{m.group(3)}", text)
    for pattern, replacement in _TOKEN_SHAPES:
        text = pattern.sub(replacement, text)
    return text


def redact_obj(obj: Any, _depth: int = 0) -> Any:
    """Recursively redact secrets in dicts/lists. Cycle-safe by depth limit."""
    if _depth > 12:
        return "[TRUNCATED]"
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, val in obj.items():
            key_str = str(key)
            if _SECRET_KEY_RE.match(key_str) or re.fullmatch(r"(?i).*(api[_-]?key|token|password|secret).*", key_str):
                out[key_str] = REDACTED
            else:
                out[key_str] = redact_obj(val, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, _depth + 1) for v in obj]
    return obj


def harden_untrusted(text: str, *, max_len: int = _MAX_LOG_LINE) -> str:
    """Make a log line / alert body safe to feed to a model.

    - strips control characters and normalises runs of whitespace
    - neutralises prompt-injection markers (kept visible as markers, not deleted)
    - redacts credentials
    - truncates long payloads
    """
    if not text:
        return ""
    clean = _CONTROL_CHARS.sub(" ", str(text))
    clean = clean.replace("\r\n", " \n ").replace("\n", " \n ")
    clean = _UNSAFE_WS.sub("  ", clean)
    clean = redact_text(clean)
    for pattern, replacement in _INJECTION_PATTERNS:
        clean = pattern.sub(replacement, clean)
    if len(clean) > max_len:
        clean = clean[: max_len - 1].rstrip() + "…"
    return clean.strip()


def contains_injection(text: str) -> bool:
    probe = str(text or "")
    return any(p.search(probe) for p, _ in _INJECTION_PATTERNS)


def wrap_untrusted(text: str, label: str) -> str:
    """Explicitly fence untrusted data so a prompt can reference it safely."""
    return f"<untrusted_{label}>\n{text}\n</untrusted_{label}>"


def scrub_exception(exc: BaseException) -> str:
    """Exception text safe for logs/API responses."""
    return redact_text(f"{type(exc).__name__}: {exc}")[:500]
