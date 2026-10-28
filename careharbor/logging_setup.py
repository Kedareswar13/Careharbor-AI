"""UTF-8 safe logging with PII redaction.

Windows consoles default to cp1252, which crashes on emoji / Devanagari. We
reconfigure stdout/stderr to UTF-8 at startup and redact phone numbers, dates
of birth and OTP codes from every log record.
"""

from __future__ import annotations

import logging
import re
import sys

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    (re.compile(r"\b[\w.-]{2,}@(?:ok)?(?:upi|ybl|paytm|axl|ibl|sbi|icici|hdfcbank|apl|okaxis|oksbi|okhdfcbank|okicici)\b", re.I), "[UPI]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"), "[IFSC]"),
    (re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{1,4}(?!\d)"), "[CARD]"),
    (re.compile(r"(?<!\d)[2-9]\d{3}\s?\d{4}\s?\d{4}(?!\d)"), "[AADHAAR]"),
    (re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"), "[PHONE]"),
    (re.compile(r"(?i)\b(a/?c|account|acct)\W{0,3}(no\.?|number)?\W{0,3}\d{9,18}\b"), "[BANK_ACCOUNT]"),
    (re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b(?!T)"), "[DATE]"),
    (re.compile(r"\b(?:19|20)\d{2}-\d{1,2}-\d{1,2}\b(?!T)"), "[DATE]"),
    (re.compile(r"\b\d{1,2}[/.\-]\d{1,2}[/.\-](?:19|20)\d{2}\b"), "[DATE]"),
    # Dates of birth written with month names ("14 May 1990", "May 14, 1990"). Birth years only (<= 2019), so the
    # appointment dates a patient types ("14 Nov 2026") stay readable in the saved conversation.
    (re.compile(r"(?i)\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s+(?:19\d{2}|20[01]\d)\b"), "[DATE]"),
    (re.compile(r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+(?:19\d{2}|20[01]\d)\b"), "[DATE]"),
    (re.compile(r"(?i)\b(otp|code|pin)\b([^\d\n]{0,30}?)(?<![\d-])\d{3}[\s-]?\d{3}(?![\d-])"), r"\1\2[OTP]"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "allow_pii", False):
            return True
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging crash the app
            return True
        record.msg = redact(msg)
        record.args = None
        return True


def force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


_configured = False


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    force_utf8_stdio()
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "urllib3", "openai", "mcp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
