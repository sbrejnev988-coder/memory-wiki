"""memory-wiki v1.2.1: native Hermes active-memory wiki vault.

Stdlib-only, Android/proot friendly. Storage: SQLite + Markdown under
$HERMES_HOME/memory-wiki. Runs inside MemoryProvider lifecycle, so recall is
near prompt building and session lifecycle, not bolted on as MCP.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
import shutil
import zipfile
import stat
import urllib.request
import urllib.error

try:
    from agent.memory_provider import MemoryProvider
except Exception:
    class MemoryProvider:  # fallback for standalone tests/tool introspection
        def __init__(self, *a, **k): pass
try:
    from tools.registry import tool_error, tool_result
except Exception:
    def tool_result(*args, **kwargs):
        if args and isinstance(args[0], dict) and not kwargs: return json.dumps(args[0], ensure_ascii=False)
        return json.dumps(kwargs or (args[0] if args else {}), ensure_ascii=False)
    def tool_error(message): return json.dumps({"success": False, "error": str(message)}, ensure_ascii=False)

WORD_RE = re.compile(r"[\wА-Яа-яЁё-]{3,}", re.UNICODE)
SENT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
PATH_RE = re.compile(r"(?:~?/|/[\w.\-]+|[A-Za-z]:\\)[\w./\\\-]+")
URL_RE = re.compile(r"https?://\S+")
SYSTEM_NOTE_RE = re.compile(r"\[\s*System note\s*:[\s\S]*?\]", re.I)
MEMORY_CONTEXT_RE = re.compile(r"<memory-context>[\s\S]*?</memory-context>", re.I)
MODEL_SWITCH_ARTIFACT_RE = re.compile(
    r"(?is)(?:\[\s*note:\s*)?model was just switched from [^\n\]]{1,240}(?:adjust your self-identification accordingly\.?)?\]?"
)
TRANSIENT_API_ARTIFACT_RE = re.compile(
    r"(?i)(?:api failed after \d+ retries|websocket connection failed|ws до codex api сломан|статус:\s*🔴)"
)
TOOL_ARTIFACT_HINT_RE = re.compile(
    r"(?i)(?:previous turn was interrupted|conversation history contains tool outputs|active memory wiki recall|"
    r"secret vault index matches|contradictions to handle explicitly|why_believe:|\[hermes\s+(?:proxy|tool)|tool\s*:?.{0,40}output\s+truncated|output\s+truncated|"
    r"upstream response\.(?:empty|failed)|no parsed (?:assistant text|output_text)|servers are currently overloaded|"
    r"exit_code|stdout|stderr|traceback|pytest|assertionerror|attributeerror|typeerror|\[called:|\[tool\]:|\[assistant\]:)"
)
PATH_ONLY_RE = re.compile(r"^`?/?[\w./\\-]+\.(?:md|py|json|ya?ml|toml|log|txt|sh)`?$", re.I)
RAW_BLOB_HINT_RE = re.compile(r"(?i)(?:^\{|\\n\s*\d+\||session_20\d{6}|/tmp/hermes_session_index_corpus|raw preview|background process proc_|full output:|tests/.+\.py:\d+)")
STALE_DAYS = 30
MAX_PREFETCH_CHARS = int(os.environ.get("MEMORY_WIKI_MAX_PREFETCH_CHARS", "12000"))
MAX_RENDER_CLAIMS_PER_TOPIC = 500
MAX_RENDER_TOPICS = 250
MIN_AUTO_INGEST_SCORE = 2
MIN_EXPLICIT_INGEST_SCORE = 1
CANONICAL_TOPICS = {
    "preferences", "hermes", "memory-wiki", "server", "android", "openclaw", "proxy", "api", "telegram",
    "config", "database", "github", "project-scoping", "secrets", "projects", "tasks", "lessons", "decisions",
    "operations", "bridge", "ibkr-zorro", "heart", "smoke", "general",
}
FORBIDDEN_AUTO_TOPICS = {
    "curl", "post", "get", "put", "patch", "delete", "noop", "test", "tests", "тест", "возвращает",
    "http", "https", "localhost", "local", "ok", "added", "workspace", "автоматически", "api-v1", "v1",
}
BAD_TOPICS = {
    "general", "root", "example", "начал", "рамках", "19000", "пользователь", "assistant", "user",
    "500", "523", "256000", "9000", "192", "127", "места", *FORBIDDEN_AUTO_TOPICS,
}
VALID_CLAIM_STATUSES = {"active", "retired", "superseded", "uncertain"}
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
TOPIC_ALIASES = {
    "memory": "hermes", "память": "hermes", "плагин": "hermes", "plugins": "hermes",
    "sqlite3": "database", "db": "database", "бд": "database",
    "tg": "telegram", "телеграм": "telegram", "бот": "telegram",
    "termux": "android", "proot": "android",
    "vps": "server", "systemd": "server", "ssh": "server",
    "секреты": "secrets", "ключи": "secrets", "tokens": "secrets", "secret": "secrets",
    "task": "tasks", "task-capsule": "tasks", "mistake": "lessons", "lesson": "lessons",
}
PIN_MARKER = "#pinned"
MEMORY_DIRECTIVE_RE = re.compile(
    r"(?i)\b(?:remember|memorize|note(?:\s+that)?|save(?:\s+to\s+memory)?|запомни|запиши\s+в\s+память|сохрани\s+в\s+память|важно\s+запомнить)\b\s*[:—-]?\s*(.+)"
)
EPHEMERAL_RE = re.compile(
    r"(?i)\b(?:сейчас|today|now|в\s+этом\s+сообщении|this\s+message|временно|temporary|одноразово|for\s+now)\b"
)
SECRET_META_QUERY_RE = re.compile(
    r"(?i)\b(keys?|ключ(?:и|ей|а|ом)?|секрет(?:ы|ов)?|credentials?|creds|api[_ -]?(?:keys?|ключи?)|tokens?|токен(?:ы|ов|а)?|\.env|конфиг|config|интеграц)\b"
)
ENV_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
SECRET_PATTERNS = [
    # Generic human-supplied passwords in memories are often mentioned as
    # `password foo`, `пароль foo`, `login user / password foo`, or as a lone
    # fenced/code value immediately after a password sentence rather than strict
    # `key=value` assignments.  Redact those before any recall/export.
    (re.compile(r"(?is)((?:password|passwd|pass|пароль)[\s\S]{0,240}?```(?:text|bash|sh)?\s*)((?=[A-Za-z0-9_@./+=\-]*\d)[A-Za-z0-9_@./+=\-]{8,})(\s*```)") , r"\1<PASSWORD_REDACTED>\3"),
    (re.compile(r"(?i)\b(password|passwd|pass|пароль)\s+(?!(?:auth(?:entication)?|disabled|enabled|login|logins|mode|modes|field|fields|value|values|manager|protected|vault|entry|entries|ssh|path|policy|required|only|is|are|was|were|есть|нет|доступ|аутентификация)\b)([A-Za-z0-9_@./+=\-]{8,})(?=\s|$|[.,;:!?\)\]])"), r"\1 <PASSWORD_REDACTED>"),
    (re.compile(r"(?i)\b(root|Hermesusclaw|Hermes|madmax|xiaomi)\s+((?=[A-Za-z0-9_@./+=\-]*\d)[A-Za-z0-9_@./+=\-]{8,})(?=\s|$|[.,;:!?\)\]])"), r"\1 <CREDENTIAL_REDACTED>"),
    (re.compile(r"\b(?:sk|rk|pk|ak)-[A-Za-z0-9_\-]{16,}\b"), "<API_KEY_REDACTED>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "<GITHUB_TOKEN_REDACTED>"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}\b"), "<GITLAB_TOKEN_REDACTED>"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{20,}\b"), "<SLACK_TOKEN_REDACTED>"),
    (re.compile(r"\bya29\.[A-Za-z0-9_\-]{20,}\b"), "<GOOGLE_TOKEN_REDACTED>"),
    (re.compile(r"\b[A-Za-z0-9_\-]{20,}:[A-Za-z0-9_\-]{24,}\b"), "<TOKEN_REDACTED>"),
    (re.compile(r"(?i)\b(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN|GITLAB_TOKEN|SLACK_TOKEN|TELEGRAM_BOT_TOKEN)\s*[:=]\s*(['\"]?)[^\s,;#]+\2"), "<SECRET_ASSIGNMENT_REDACTED>"),
    (re.compile(r"(?i)(password|passwd|pass|пароль|token|токен|api[_ -]?key|secret|client[_ -]?secret|access[_ -]?key|private[_ -]?key|credential|credentials)\s*[:=]\s*(['\"]?)(?!sec_[0-9a-f]{12}\b)[^\s,;#\]\)]+\2"), "<SECRET_ASSIGNMENT_REDACTED>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{20,}"), "<BEARER_REDACTED>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<PRIVATE_KEY_REDACTED>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<AWS_ACCESS_KEY_REDACTED>"),
    (re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{48,}(?![A-Za-z0-9/+=])"), "<POSSIBLE_SECRET_REDACTED>"),
]
SECRET_FIELD_RE = re.compile(r"(?i)\b(password|passwd|пароль|token|токен|api[_ -]?key|secret|client[_ -]?secret|access[_ -]?key|private[_ -]?key|credential|credentials)\b")
SECRET_ASSIGN_RE = re.compile(r"(?i)\b(password|passwd|пароль|token|токен|api[_ -]?key|secret|client[_ -]?secret|access[_ -]?key|private[_ -]?key)\s*[:=]\s*([^\s,;]+)")
REDaction_MARKER_RE = re.compile(r"<[^>]*(?:REDACTED|redacted)[^>]*>|\*{3,}|•••", re.I)
REDACTION_TOKEN_RE = re.compile(r"<[^>]*(?:REDACTED|redacted)[^>]*>", re.I)
MEMORY_CLASS_WEIGHTS = {"secret": 1.0, "credential_index": 0.95, "tool_log": 0.30, "raw_blob": 0.22, "preference": 0.82, "procedure": 0.78, "environment": 0.74, "decision": 0.80, "lesson": 0.78, "fact": 0.64}
SOURCE_POLICY = {
    # Пишем только то, что имеет шанс быть долговечным. Остальное — в review queue/карантин.
    "explicit": {"default_confidence": 0.95, "candidate_only": False, "allow_preferences": True, "allow_raw": False},
    "conversation": {"default_confidence": 0.62, "candidate_only": True, "allow_preferences": True, "allow_raw": False},
    "conversation_summary": {"default_confidence": 0.55, "candidate_only": True, "allow_preferences": False, "allow_raw": False},
    "tool": {"default_confidence": 0.74, "candidate_only": False, "allow_preferences": False, "allow_raw": False},
    "config_metadata": {"default_confidence": 0.90, "candidate_only": False, "allow_preferences": False, "allow_raw": False},
    "import": {"default_confidence": 0.58, "candidate_only": True, "allow_preferences": False, "allow_raw": False},
    "curated": {"default_confidence": 0.92, "candidate_only": False, "allow_preferences": True, "allow_raw": False},
    "unknown": {"default_confidence": 0.50, "candidate_only": True, "allow_preferences": False, "allow_raw": False},
}
CURATED_SOURCES = {
    "post_task", "project_profile", "decision", "mistake", "task_capsule",
    "memory_wiki_compress_topic", "memory_wiki_compile_topic", "memory_wiki_import_bundle",
}
STOP = {
    "the","and","for","with","that","this","from","have","will","you","your","are","was","were","but","not","как","что","это","для","или","при","если","где","уже","под","над","без","его","она","они","оно"
}


def now() -> int: return int(time.time())
def sha(s: str) -> str: return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()
def short(s: str, n: int = 240) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"
def slug(s: str, n: int = 80) -> str:
    s = re.sub(r"[^\wА-Яа-яЁё-]+", "-", str(s or "").lower(), flags=re.UNICODE).strip("-")
    return (s[:n].strip("-") or "general")

def normalize_claim_status(status: str) -> str:
    """Normalize lifecycle status and quarantine unknown/corrupted values as uncertain."""
    s = slug(status or "active")
    return s if s in VALID_CLAIM_STATUSES else "uncertain"

def topic_integrity_reason(topic: str) -> str:
    """Return a compact reason when a topic is not a safe canonical slug."""
    raw = str(topic or "")
    clean = slug(raw)
    if not raw.strip():
        return "empty topic"
    if CONTROL_CHAR_RE.search(raw):
        return "control characters in topic"
    if raw.strip().lower() != clean:
        return "non-slug topic"
    if (clean in BAD_TOPICS or clean.isdigit() or len(clean) < 3) and clean not in CANONICAL_TOPICS:
        return "bad/generated topic"
    return ""

def tokens(s: str) -> set[str]: return {w.group(0).lower() for w in WORD_RE.finditer(s or "") if w.group(0).lower() not in STOP}
def age_days(ts: int) -> float: return max(0.0, (now() - int(ts or 0)) / 86400.0)
def clamp(x: float, lo=0.0, hi=1.0) -> float: return max(lo, min(hi, x))
def esc_fts_token(s: str) -> str: return '"' + str(s).replace('"', '""') + '"'
def claim_search_text(claim: str, normalized: str = "", topic: str = "", evidence: str = "") -> str:
    """One canonical search document for FTS/LIKE fallback.

    Repeat high-signal fields to approximate field weights in a tiny stdlib-only
    FTS setup: exact claim/normalized wording should beat evidence-only hits.
    Evidence is intentionally redacted before indexing so raw credential values
    from old provenance blobs cannot dominate recall or leak via FTS matches.
    """
    parts = [
        redact_secrets(scrub_memory_artifacts(claim or "")),
        redact_secrets(scrub_memory_artifacts(normalized or claim or "")),
        topic or "",
        redact_secrets(scrub_memory_artifacts(evidence or "")),
    ]
    return "\n".join([parts[0], parts[0], parts[1], parts[1], parts[2], parts[3]])

def safe_fts_query(q: str, *, max_terms: int = 8, mode: str = "or") -> str:
    terms = [esc_fts_token(t) for t in sorted(tokens(q))[:max_terms]]
    if terms:
        joiner = " AND " if mode == "and" else " OR "
        return joiner.join(terms)
    return esc_fts_token(q)


def score_breakdown(r: sqlite3.Row, q: str, qt: set[str], lexical: float, exact: float, freshness: float, recency: float, access: float, quality: float, usefulness: float, pinned: int, stale: bool) -> Dict[str, float]:
    confidence = float(r["confidence"]); salience = float(r["salience"]); trust = float(r["trust_score"] if "trust_score" in r.keys() else .55)
    risk = str(r["risk"] if "risk" in r.keys() else "low")
    lifecycle_penalty = -0.35 if stale else 0.0
    risk_penalty = {"secret": -1.20, "high": -0.60, "medium": -0.25}.get(risk, 0.0)
    artifact_penalty = 0.0
    try:
        text = str(r["claim"] or "")
        typ = str(r["type"] if "type" in r.keys() else "")
        trust_class = str(r["trust_class"] if "trust_class" in r.keys() else "")
        if is_ephemeral_fragment(text) or trust_class in ("tool_log", "raw_blob") or typ == "source_artifact":
            artifact_penalty -= 1.05
        if quality < 0.35 and not pinned:
            artifact_penalty -= 0.65
    except Exception:
        pass
    return {
        "lexical": 3.2 * lexical,
        "exact": exact,
        "confidence": 1.0 * confidence,
        "salience": 1.2 * salience,
        "freshness": 0.65 * freshness,
        "recency": 0.25 * recency,
        "access": access,
        "quality": 0.95 * quality,
        "usefulness": 0.55 * usefulness,
        "trust": 0.90 * trust,
        "pinned": 0.65 if pinned else 0.0,
        "stale_penalty": lifecycle_penalty,
        "risk_penalty": risk_penalty,
        "artifact_penalty": artifact_penalty,
    }

def bm25_norm(x: float) -> float:
    # SQLite bm25() returns lower-is-better and often negative values. Convert to
    # small positive boost without letting it dominate trust/lifecycle signals.
    try:
        return max(0.0, min(0.8, -float(x) / 8.0))
    except Exception:
        return 0.0

def atomic_write(path: Path, text: str) -> None:
    """Crash-safe text write: temp file + fsync + atomic rename + dir fsync when possible."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        try:
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        except Exception:
            pass
    except Exception:
        try: os.unlink(tmp_name)
        except Exception: pass
        raise

def safe_join(base: Path, *parts: str) -> Path:
    base_abs = Path(base).expanduser().resolve()
    path = base_abs.joinpath(*[str(p) for p in parts]).resolve()
    if path != base_abs and base_abs not in path.parents:
        raise ValueError(f"path escapes memory-wiki root: {path}")
    return path

def zip_member_safe(name: str) -> bool:
    raw = str(name or "").replace("\\", "/").strip()
    if not raw:
        return False
    p = Path(raw)
    return not p.is_absolute() and ".." not in p.parts

def zip_member_regular(info: zipfile.ZipInfo) -> bool:
    """Only restore regular files; never trust symlink/device entries from archives."""
    mode = (int(getattr(info, "external_attr", 0) or 0) >> 16) & 0o170000
    return mode in (0, stat.S_IFREG)

def redact_secrets(text: str) -> str:
    s = str(text or "")
    for pat, repl in SECRET_PATTERNS:
        s = pat.sub(repl, s)
    return s

def scrub_memory_artifacts(text: str) -> str:
    """Drop injected system/tool context that should not become durable memory."""
    s = str(text or "")
    s = SYSTEM_NOTE_RE.sub(" ", s)
    s = MEMORY_CONTEXT_RE.sub(" ", s)
    s = MODEL_SWITCH_ARTIFACT_RE.sub(" ", s)
    s = TRANSIENT_API_ARTIFACT_RE.sub(" ", s)
    return s

def secret_scan(text: str) -> Dict[str, Any]:
    """Classify secret exposure without returning raw secret values."""
    raw = str(text or "")
    redacted = redact_secrets(raw)
    findings: List[Dict[str, Any]] = []
    for pat, repl in SECRET_PATTERNS:
        for m in pat.finditer(raw):
            findings.append({"kind": repl.strip("<>").lower(), "span": [m.start(), m.end()], "sample": short(redact_secrets(m.group(0)), 80)})
    marker_spans = [m.span() for m in re.finditer(r"\[REDACTED_SECRET(?::[^\]]+)?\]", raw, re.I)]
    for m in SECRET_ASSIGN_RE.finditer(raw):
        if any(m.start() >= a and m.end() <= b for a, b in marker_spans):
            continue
        findings.append({"kind": "secret_assignment", "field": m.group(1), "span": [m.start(), m.end()], "sample": f"{m.group(1)}=<REDACTED>"})
    redaction_markers = bool(REDaction_MARKER_RE.search(raw))
    suspicious_name = bool(SECRET_FIELD_RE.search(raw))
    raw_secret = bool(findings) or redacted != raw
    risk = 0.0
    if raw_secret: risk += 0.90
    if suspicious_name: risk += 0.22
    if redaction_markers and not raw_secret: risk += 0.08
    if "secret index:" in raw.lower() or "<stored in secret_index>" in raw: risk = max(risk, 0.10)
    return {"raw_secret": raw_secret, "mentions_secret": suspicious_name, "redaction_markers": redaction_markers, "risk": round(clamp(risk), 3), "findings": findings[:12], "redacted": redacted}

def memory_classify(text: str, topic: str = "", source: str = "") -> Dict[str, Any]:
    blob = f"{topic} {source} {text}".lower()
    scan = secret_scan(text)
    cls = "fact"
    if scan["raw_secret"]:
        cls = "secret"
    elif "secret index:" in blob or "secret_index" in blob:
        cls = "credential_index"
    elif "traceback" in blob or "stdout" in blob or "stderr" in blob or "tool output" in blob or len(str(text or "")) > 1800:
        cls = "tool_log" if len(str(text or "")) <= 3500 else "raw_blob"
    elif any(k in blob for k in ("prefers", "preference", "предпочитает", "нравится", "не любит")):
        cls = "preference"
    elif any(k in blob for k in ("procedure", "workflow", "команд", "запуск", "deploy", "backup", "restore")):
        cls = "procedure"
    elif any(k in blob for k in ("path", "port", "installed", "config", "env", "service", "android", "termux", "сервер", "конфиг")):
        cls = "environment"
    elif any(k in blob for k in ("decision", "решение", "rationale")):
        cls = "decision"
    elif any(k in blob for k in ("mistake", "lesson", "ошибка", "урок", "prevention")):
        cls = "lesson"
    trust = MEMORY_CLASS_WEIGHTS.get(cls, 0.55)
    if scan["raw_secret"]: trust = 0.05
    if cls in ("tool_log", "raw_blob"): trust = min(trust, 0.35)
    return {"class": cls, "trust": round(clamp(trust), 3), "risk": scan["risk"], "secret_scan": scan}

def _looks_sensitive_env_name(name: str) -> bool:
    n = str(name or "").upper()
    return any(x in n for x in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH"))

def _env_value_state(name: str, value: str) -> str:
    if not str(value or ""):
        return "empty"
    if _looks_sensitive_env_name(name):
        return "set/redacted"
    return "set"

def source_policy_for(source: str) -> Dict[str, Any]:
    """Stable source-ingestion policy used by write firewall and provenance cards."""
    src = str(source or "").strip()
    st = infer_source_type(src)
    if src.startswith("phase6_curated_summary") or src in CURATED_SOURCES or src.startswith("memory_wiki_"):
        st = "curated"
    policy = dict(SOURCE_POLICY.get(st, SOURCE_POLICY["unknown"]))
    policy["source"] = src
    policy["source_type"] = st
    return policy

def normalize_claim(text: str) -> str:
    s = redact_secrets(str(text or ""))
    s = re.sub(r"\s+", " ", s).strip(" -•\t\r\n")
    s = re.sub(r"^(User|Assistant|System|Tool)\s*:\s*", "", s, flags=re.I)
    return short(s, 1400)

def extract_memory_directive(text: str) -> str:
    """Return the durable part of an explicit memory instruction, if present."""
    m = MEMORY_DIRECTIVE_RE.search(str(text or ""))
    if not m:
        return ""
    return normalize_claim(m.group(1))

def is_ephemeral_fragment(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    low = s.lower()
    if re.match(r"(?i)^(memory-wiki quality policy|secrets memory summary|server environment summary|telegram summary|proxy/api summary|openclaw summary|hermes operational memory summary|user workflow preferences summary|topic summary for )", s):
        return False
    if low.startswith(("tool results were processed", "[hermes proxy]", "{", "system note:", "previous turn was interrupted")):
        return True
    if MODEL_SWITCH_ARTIFACT_RE.search(s) or TRANSIENT_API_ARTIFACT_RE.search(s):
        return True
    if RAW_BLOB_HINT_RE.search(s):
        return True
    tool_hit = TOOL_ARTIFACT_HINT_RE.search(s)
    if tool_hit and ("\\n" in s or "[tool]" in low or "[called:" in low or "traceback" in low or "stdout" in low or "stderr" in low or "exit_code" in low):
        return True
    if PATH_ONLY_RE.fullmatch(s):
        return True
    if s.count("\n") > 6 or len(s) > 1800:
        return True
    if any(x in low for x in ("запусти команду", "run command", "покажи лог", "show log", "исправь ошибку сейчас")):
        return True
    return False


def memory_gate_decision(text: str, topic: str = "", source: str = "") -> Dict[str, Any]:
    """Write-time quality gate: accept durable claims, queue noisy/borderline input."""
    s = normalize_claim(text)
    lint = lint_claim_text(s, topic)
    low = f"{s} {topic} {source}".lower()
    source_s = str(source or "")
    policy = source_policy_for(source_s)
    explicit = source_s.startswith("memory_tool") or source_s in ("tool", "explicit_user_correction")
    curated = source_s.startswith("phase6_curated_summary") or source_s in CURATED_SOURCES or policy.get("source_type") == "curated"
    durable_hint = any(k in low for k in (
        "user prefers", "пользователь предпочитает", "preference", "procedure", "runbook", "decision",
        "verified", "проверено", "installed", "установ", "service", "systemd", "config", "endpoint",
        "task capsule", "post_task", "backup", "restore", "environment", "сервер", "android", "hermes"
    ))
    if secret_scan(s).get("raw_secret"):
        return {"action":"redact", "reason":"raw secret-like material", "lint":lint}
    if "forbidden generated topic" in lint["issues"]:
        return {"action":"queue", "reason":"forbidden generated topic", "lint":lint}
    if is_ephemeral_fragment(s):
        return {"action":"reject", "reason":"system/tool artifact or raw blob", "lint":lint}
    if "raw blob/log; summarize first" in lint["issues"] or "raw log/json blob" in lint["issues"]:
        if source_s == "task_capsule" or curated:
            return {"action":"queue", "reason":"structured data should be summarized before claim storage", "lint":lint}
        return {"action":"reject", "reason":"raw blob/log; summarize first", "lint":lint}
    if policy.get("candidate_only") and not (explicit or curated or durable_hint):
        return {"action":"queue", "reason":"source policy requires review", "lint":lint, "policy":policy}
    if lint["quality"] < 0.34 or (lint["issues"] and not durable_hint and not curated):
        return {"action":"queue", "reason":"low quality or ambiguous durability", "lint":lint, "policy":policy}
    if lint["quality"] < 0.48 and not (explicit or durable_hint or curated):
        return {"action":"queue", "reason":"borderline quality", "lint":lint}
    return {"action":"accept", "reason":"accepted", "lint":lint, "policy":policy}

def infer_source_type(source: str) -> str:
    src = str(source or "").lower()
    if src.startswith("memory_tool") or src == "tool": return "explicit"
    if src.startswith("turn:"): return "conversation"
    if src.startswith("session_end") or src == "pre_compress": return "conversation_summary"
    if "env-metadata" in src: return "config_metadata"
    if "import" in src: return "import"
    return "tool"

def infer_claim_type(text: str, topic: str = "") -> str:
    low = str(text or "").lower()
    if any(x in low for x in ("prefers", "preference", "предпоч", "любит", "не любит")): return "preference"
    if any(x in low for x in ("do not", "never", "нельзя", "никогда", "не надо", "don't")): return "constraint"
    if any(x in low for x in ("step ", "шаг", "procedure", "инструкция", "to update", "как обнов")): return "procedure"
    if PATH_RE.search(low) or any(x in low for x in ("installed", "установ", "port", "service", "systemd", "конфиг", "config", ".env")): return "environment"
    if any(x in low for x in ("decided", "решил", "решение", "выбрали")): return "decision"
    return "fact"



def canonical_topic(topic: str, claim: str = "") -> str:
    """Rule-based stable topic normalization; avoids noisy one-word topics.

    Explicit canonical topics win over keyword sniffing. This prevents summary/preferences
    claims from being dragged into android/hermes merely because they mention tools.
    """
    t = slug(topic or "general")
    low_topic = str(topic or "").lower()
    low_claim = str(claim or "").lower()
    low = f"{low_topic} {low_claim}"
    if t in TOPIC_ALIASES:
        return TOPIC_ALIASES[t]
    if t in CANONICAL_TOPICS and t not in BAD_TOPICS:
        return t
    if t in FORBIDDEN_AUTO_TOPICS or t.isdigit() or len(t) < 3:
        return "general"
    if "preference" in low_topic or low_claim.startswith(("user workflow preferences", "user preferences", "пользователь предпочитает")):
        return "preferences"
    if "secret" in low_topic or "credential" in low_topic or low_claim.startswith("secrets memory summary"):
        return "secrets"
    if "memory-wiki" in low_topic or "memory_wiki" in low_topic or "memory wiki" in low_topic:
        return "memory-wiki"
    if "memory-wiki" in low_claim or "memory_wiki" in low_claim or "memory wiki" in low_claim:
        return "memory-wiki"
    if "openclaw" in low: return "openclaw"
    if "telegram" in low or "bot token" in low: return "telegram"
    if "android" in low or "termux" in low or "proot" in low: return "android"
    if "sqlite" in low or "database" in low: return "database"
    if t not in CANONICAL_TOPICS and (len(t) < 5 or t in BAD_TOPICS):
        return "general"
    return t

def infer_scope(text: str, source: str = "", topic: str = "") -> str:
    low = f"{text} {source} {topic}".lower()
    if source.startswith("turn:") or source in ("pre_compress",):
        if any(x in low for x in ("сейчас", "temporary", "this session", "текущ", "лог", "команду")): return "session"
    if any(x in low for x in ("project", "workspace", "repo", "/workspace/", "openclaw", "memory-wiki", "проект")): return "project"
    if any(x in low for x in ("android", "termux", "proot", "server", "vps", "path", "installed", "порт", "port")): return "device"
    if any(x in low for x in ("user prefers", "пользователь предпочитает", "не надо", "do not", "never", "нравится")): return "user"
    return "global"

def current_project_id() -> str:
    cwd = os.getcwd()
    remote = ""
    try:
        gp = Path(cwd) / ".git" / "config"
        if gp.exists():
            m = re.search(r"url\s*=\s*(.+)", gp.read_text(encoding="utf-8", errors="ignore"))
            remote = m.group(1).strip() if m else ""
    except Exception:
        pass
    seed = remote or cwd
    name = slug(Path(cwd).name or "project", 32)
    return f"{name}-{sha(seed)[:8]}"


def lint_claim_text(text: str, topic: str = "") -> Dict[str, Any]:
    s = normalize_claim(text); issues=[]; suggestions=[]
    ts = tokens(s)
    if len(s) < 18 or len(ts) < 3: issues.append("too short")
    if len(s) > 1400: issues.append("too long")
    low = s.lower(); topic_slug = slug(topic)
    if low.startswith(("assistant:", "tool results were processed", "[hermes proxy]", "[hermes tool]")) or is_ephemeral_fragment(s): issues.append("assistant/tool artifact")
    if PATH_ONLY_RE.fullmatch(s): issues.append("path-only fragment")
    if re.fullmatch(r"[{}\[\]0-9\s:,.\"'_-]+", s): issues.append("non-semantic blob")
    if s.count("\\n") > 8 or (s.startswith("{") and len(s) > 400) or RAW_BLOB_HINT_RE.search(s): issues.append("raw log/json blob")
    if redact_secrets(s) != s: issues.append("contains secret-like material"); suggestions.append("redact or retire")
    if topic_slug in FORBIDDEN_AUTO_TOPICS: issues.append("forbidden generated topic"); suggestions.append(f"topic -> {canonical_topic(topic, s)}")
    if topic_slug in BAD_TOPICS or str(topic).isdigit() or len(topic_slug) < 3: issues.append("bad topic"); suggestions.append(f"topic -> {canonical_topic(topic, s)}")
    if len(ts) < 4: issues.append("too few semantic tokens")
    if s.startswith("{") or s.count("\\n") > 3: issues.append("raw blob/log; summarize first")
    q = claim_quality(s, canonical_topic(topic, s))
    if q < .35: suggestions.append("rewrite as one durable fact/preference/procedure")
    return {"ok": not issues and q >= .35, "quality": q, "issues": issues, "suggestions": suggestions, "normalized": s, "topic": canonical_topic(topic, s), "type": infer_claim_type(s, topic), "scope": infer_scope(s, "", topic)}

def is_bad_claim_fragment(text: str) -> Tuple[bool, str]:
    s = str(text or "").strip(); low = s.lower(); ts = tokens(s)
    if len(s) < 18 or len(ts) < 3: return True, "too short"
    if len(s) > 1400: return True, "too long"
    if low.startswith(("если ", "if ", "то есть ", "сейчас есть ", "now there is ", "например", "example")) and not (PATH_RE.search(s) or URL_RE.search(s)):
        return True, "contextual fragment"
    if low.startswith(("assistant:", "tool results were processed", "[hermes proxy]", "[hermes tool]")) or is_ephemeral_fragment(s):
        return True, "assistant/tool artifact"
    if re.fullmatch(r"[{}\[\]0-9\s:,.\"'_-]+", s): return True, "non-semantic blob"
    if s.count("\\n") > 8 or (s.startswith("{") and len(s) > 400): return True, "raw log/json blob"
    return False, ""


def claim_quality(text: str, topic: str = "") -> float:
    s = normalize_claim(text); ts = tokens(s); low = s.lower(); score = 0.0
    score += min(0.26, len(ts) / 90.0)
    if 25 <= len(s) <= 420: score += 0.22
    if any(x in low for x in ("prefers", "uses", "installed", "server", "path", "пользователь", "сервер", "установ", "предпоч", "config", "конфиг", "service")): score += 0.18
    if PATH_RE.search(s) or URL_RE.search(s): score += 0.10
    topic_slug = canonical_topic(topic, s)
    if topic_slug and topic_slug not in BAD_TOPICS and not str(topic_slug).isdigit(): score += 0.14
    bad, _ = is_bad_claim_fragment(s)
    if bad: score -= 0.38
    if topic_slug in BAD_TOPICS or str(topic_slug).isdigit(): score -= 0.25
    if topic_slug in FORBIDDEN_AUTO_TOPICS: score -= 0.35
    if re.search(r"<[^>]*REDACTED>", redact_secrets(s)): score -= 0.08
    if re.match(r"^[/\w.\-]+\s+\d{3}\s+\w+:\w+$", s): score -= 0.18
    return round(clamp(score), 3)


class MemoryWikiProvider(MemoryProvider):
    @property
    def name(self) -> str: return "memory-wiki"

    def __init__(self) -> None:
        self.home = Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")).expanduser()
        self.root = self.home / "memory-wiki"
        self.pages_dir = self.root / "pages"
        self.dashboard_dir = self.root / "dashboards"
        self.backups_dir = self.root / "backups"
        self.snapshots_dir = self.root / "snapshots"
        self.db_path = self.root / "memory_wiki.sqlite3"
        self.spool_dir = self.root / "spool"
        self.recovery_dir = self.root / "recovery"
        self.session_id = "default"
        self.platform = ""
        self.agent_context = "primary"
        self.turn = 0
        self._conn: Optional[sqlite3.Connection] = None
        self._degraded = False
        self._last_io_error = ""
        self._lock = threading.RLock()

    # ----- lifecycle -----------------------------------------------------
    def is_available(self) -> bool: return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self.session_id = session_id or "default"
        self.platform = kwargs.get("platform") or ""
        self.agent_context = kwargs.get("agent_context") or "primary"
        if kwargs.get("hermes_home"):
            self.home = Path(kwargs["hermes_home"]).expanduser()
            self.root = self.home / "memory-wiki"
            self.pages_dir = self.root / "pages"
            self.dashboard_dir = self.root / "dashboards"
            self.backups_dir = self.root / "backups"
            self.snapshots_dir = self.root / "snapshots"
            self.db_path = self.root / "memory_wiki.sqlite3"
            self.spool_dir = self.root / "spool"
            self.recovery_dir = self.root / "recovery"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.dashboard_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        self._connect(); self._migrate(); self._rebuild_fts(); self._sync_env_metadata(); self._render_all()

    def system_prompt_block(self) -> str:
        return (
            "# Memory-Wiki Active Memory\n"
            "Persistent memory is a local wiki vault of structured claims with evidence, confidence, salience, freshness, and contradiction tracking. "
            "Relevant claims appear in <memory-context> before the main answer. Treat ACTIVE high-confidence claims as durable context; prefer fresher or better-evidenced claims when conflict exists. "
            "Use memory_wiki_* tools to query, add evidence, resolve contradictions, inspect dashboards, merge duplicates, export/import backups, or maintain the vault. "
            "Do not treat low-confidence/stale recall as user input; verify or refresh it before relying on it. "
            "Use typed memory: credential/secret records belong in the secret vault index, procedures in procedure claims, and task outcomes through post-task hooks."
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self.turn = int(turn_number or self.turn or 0)
        if self.turn and self.turn % 15 == 0:
            self._maintenance()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        plan = self._recall_plan(query, limit=6)
        rows = self._search(query, limit=10, include_stale=True)
        env_meta = self._env_metadata_context(query)
        secrets_meta = self._secret_context(query, limit=3)
        if not rows and not env_meta and not plan.get("topics") and not secrets_meta: return ""
        lines = ["## Active Memory Wiki Recall", "Use these as durable background, not new user input. Prefer active, fresh, high-confidence claims; refresh or verify stale/uncertain items. Secret values are never included; use the secret vault only when explicitly required."]
        if plan.get("topics"):
            lines.append("Recall plan: topics=" + ", ".join(plan.get("topics", [])[:6]) + "; types=" + ", ".join(plan.get("types", [])[:6]))
        if env_meta:
            lines.append(env_meta)
        if secrets_meta:
            lines.append(secrets_meta)
        for r in rows:
            flags = []
            if self._is_stale(r["freshness_at"]): flags.append("STALE")
            if r["status"] != "active": flags.append(r["status"].upper())
            tag = f" [{' '.join(flags)}]" if flags else ""
            pin = ' 📌' if int(r.get('pinned') or 0) else ''
            claim_text = redact_secrets(r['claim'])
            cls = r.get('memory_class') or memory_classify(claim_text, r.get('topic','')).get('class','fact')
            trust = float(r.get('trust_score', memory_classify(claim_text, r.get('topic','')).get('trust', .5)) or .5)
            why = r.get('why_believe') or f"source={r.get('source','')}; evidence_count={r.get('evidence_count',0)}"
            lines.append(f"- `{r['id']}` {tag}{pin} class={cls} trust={trust:.2f} topic={r['topic']} conf={r['confidence']:.2f} sal={r['salience']:.2f} score={r.get('score',0):.2f}: {claim_text}")
            lines.append(f"  why_believe: {short(redact_secrets(why), 180)}")
            ev = self._top_evidence(r["id"], 1)
            if ev: lines.append(f"  evidence: {short(redact_secrets(ev[0]['text']), 180)}")
        cons = self._related_contradictions([r["id"] for r in rows], limit=4)
        if cons:
            lines.append("\nContradictions to handle explicitly:")
            for c in cons: lines.append(f"- `{c['id']}` {redact_secrets(c['claim_a'])} ↔ {redact_secrets(c['claim_b'])}: {redact_secrets(c['reason'])} [{c['status']}]")
        out = "\n".join(lines)
        return out[:MAX_PREFETCH_CHARS]

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None: return None

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if self.agent_context not in ("primary", "foreground", ""): return
        sid = session_id or self.session_id
        clean_user = scrub_memory_artifacts(user_content)
        clean_assistant = scrub_memory_artifacts(assistant_content)
        self._ingest_text(clean_user, source=f"turn:user:{sid}", max_claims=8)
        self._ingest_text(clean_assistant, source=f"turn:assistant:{sid}", max_claims=2)

    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        if action in ("add", "replace") and content:
            self._add_claim(content, topic=target or self._infer_topic(content), evidence=json.dumps(metadata or {}, ensure_ascii=False), source=f"memory_tool:{action}:{target}", confidence=0.86, salience=0.88)
        elif action == "remove" and content:
            self._set_status_by_text(content, "retired", f"memory_tool:{action}:{target}")

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        text = scrub_memory_artifacts("\n".join(str(m.get("content", ""))[:3000] for m in messages[-16:]))
        self._ingest_text(text, source="pre_compress", max_claims=10)
        rows = self._search(text, limit=12, include_stale=True)
        if not rows: return ""
        return "Memory-Wiki claims to preserve during compression:\n" + "\n".join(f"- `{r['id']}` {r['claim']}" for r in rows)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        text = "\n".join(str(m.get("content", ""))[:4000] for m in messages[-24:])
        self._ingest_text(text, source=f"session_end:{self.session_id}", max_claims=14)
        self._maintenance(); self._render_all()

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False, **kwargs) -> None:
        self.session_id = new_session_id or self.session_id
        if reset: self._maintenance()

    def shutdown(self) -> None:
        if self._conn:
            self._render_all(); self._conn.close(); self._conn = None

    # ----- tools ---------------------------------------------------------
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        P = lambda props, req=(): {"type":"object","properties":props,"required":list(req)}
        return [
            {"name":"memory_wiki_query","description":"Search memory-wiki claims with FTS + salience/freshness scoring.","parameters":P({"query":{"type":"string"},"limit":{"type":"integer","default":10},"include_stale":{"type":"boolean","default":True},"topic":{"type":"string"}}, ["query"])},
            {"name":"memory_wiki_add_claim","description":"Add/update a structured durable claim.","parameters":P({"claim":{"type":"string"},"topic":{"type":"string","default":"general"},"evidence":{"type":"string","default":""},"source":{"type":"string","default":"tool"},"confidence":{"type":"number","default":0.75},"salience":{"type":"number","default":0.7}}, ["claim"])},
            {"name":"memory_wiki_add_secret","description":"Add/update a structured credential/secret index entry. Values are stored locally with redacted recall summaries.","parameters":P({"subject":{"type":"string"},"scope":{"type":"string"},"secret_type":{"type":"string","default":"credential"},"locator":{"type":"string","default":""},"value":{"type":"string","default":""},"purpose":{"type":"string","default":""},"source":{"type":"string","default":"tool"},"confidence":{"type":"number","default":0.85},"salience":{"type":"number","default":0.85}}, ["subject","scope"])},
            {"name":"memory_wiki_query_secrets","description":"Query secret vault index. By default returns redacted values; set reveal=true only when the task explicitly requires the secret.","parameters":P({"query":{"type":"string"},"limit":{"type":"integer","default":10},"reveal":{"type":"boolean","default":False}}, ["query"])},
            {"name":"memory_wiki_recall_plan","description":"Plan which topics/types/secrets should be recalled for a query.","parameters":P({"query":{"type":"string"},"limit":{"type":"integer","default":8}}, ["query"])},
            {"name":"memory_wiki_post_task","description":"Record a post-task durable summary with changed files, backups, verification and service restarts.","parameters":P({"summary":{"type":"string"},"topic":{"type":"string","default":"operations"},"changed_files":{"type":"array","items":{"type":"string"}},"backups":{"type":"array","items":{"type":"string"}},"verification":{"type":"string","default":""},"services":{"type":"array","items":{"type":"string"}},"source":{"type":"string","default":"post_task"}}, ["summary"])},
            {"name":"memory_wiki_active_dashboard","description":"Render/read active operational memory dashboard.","parameters":P({"limit":{"type":"integer","default":80}}, [])},
            {"name":"memory_wiki_doctor","description":"Run diagnostics over schema, FTS, dashboards, backups, secrets, contradictions and recall health.","parameters":P({"repair":{"type":"boolean","default":False}}, [])},
            {"name":"memory_wiki_backup","description":"Create a full zip backup of sqlite/pages/dashboards/metadata.","parameters":P({"reason":{"type":"string","default":"manual"}}, [])},
            {"name":"memory_wiki_list_backups","description":"List memory-wiki backups.","parameters":P({"limit":{"type":"integer","default":20}}, [])},
            {"name":"memory_wiki_restore","description":"Restore memory-wiki from a backup id/path.","parameters":P({"backup":{"type":"string"}}, ["backup"])},
            {"name":"memory_wiki_add_decision","description":"Record an architectural/product decision with rationale and alternatives.","parameters":P({"decision":{"type":"string"},"rationale":{"type":"string","default":""},"topic":{"type":"string","default":"decisions"},"alternatives":{"type":"array","items":{"type":"string"}},"source":{"type":"string","default":"tool"}}, ["decision"])},
            {"name":"memory_wiki_add_mistake","description":"Record an anti-regression mistake/lesson with trigger, fix and prevention.","parameters":P({"trigger":{"type":"string"},"mistake":{"type":"string"},"fix":{"type":"string","default":""},"prevention":{"type":"string","default":""},"topic":{"type":"string","default":"lessons"}}, ["trigger","mistake"])},
            {"name":"memory_wiki_add_project_profile","description":"Add/update a project profile: root, purpose, commands, services, notes.","parameters":P({"project_id":{"type":"string"},"root":{"type":"string","default":""},"purpose":{"type":"string","default":""},"commands":{"type":"array","items":{"type":"string"}},"services":{"type":"array","items":{"type":"string"}},"notes":{"type":"string","default":""}}, ["project_id"])},
            {"name":"memory_wiki_add_task_capsule","description":"Record a rich task capsule with intent, plan, files, commands, errors, fixes, verification, followups.","parameters":P({"intent":{"type":"string"},"topic":{"type":"string","default":"tasks"},"plan":{"type":"string","default":""},"files":{"type":"array","items":{"type":"string"}},"commands":{"type":"array","items":{"type":"string"}},"errors":{"type":"array","items":{"type":"string"}},"fixes":{"type":"array","items":{"type":"string"}},"verification":{"type":"string","default":""},"followups":{"type":"array","items":{"type":"string"}}}, ["intent"])},
            {"name":"memory_wiki_add_entity","description":"Add/update entity and aliases for lightweight knowledge graph.","parameters":P({"name":{"type":"string"},"entity_type":{"type":"string","default":"thing"},"aliases":{"type":"array","items":{"type":"string"}},"notes":{"type":"string","default":""}}, ["name"])},
            {"name":"memory_wiki_add_relation","description":"Add a relation edge between entities.","parameters":P({"subject":{"type":"string"},"predicate":{"type":"string"},"object":{"type":"string"},"confidence":{"type":"number","default":0.8},"evidence":{"type":"string","default":""}}, ["subject","predicate","object"])},
            {"name":"memory_wiki_graph_query","description":"Query lightweight entity graph around an entity/text.","parameters":P({"query":{"type":"string"},"limit":{"type":"integer","default":20}}, ["query"])},
            {"name":"memory_wiki_apply_user_correction","description":"Capture user correction, supersede/uncertain matching old claims, and add corrected claim.","parameters":P({"correction":{"type":"string"},"target_claim_id":{"type":"string","default":""},"topic":{"type":"string","default":"corrections"}}, ["correction"])},
            {"name":"memory_wiki_pack_context","description":"Budget-aware recall/context packing for a query.","parameters":P({"query":{"type":"string"},"max_chars":{"type":"integer","default":3800}}, ["query"])},
            {"name":"memory_wiki_migrate_secrets_from_claims","description":"Best-effort migration of secret-like claims into secret_index.","parameters":P({"apply":{"type":"boolean","default":True},"limit":{"type":"integer","default":100}}, [])},
            {"name":"memory_wiki_scrub_secrets","description":"Scan memory tables for raw secrets, create secret_index metadata, and redact/quarantine matching fields.","parameters":P({"apply":{"type":"boolean","default":False},"limit":{"type":"integer","default":200}}, [])},
            {"name":"memory_wiki_snapshot","description":"Write a human-readable snapshot markdown of active memory.","parameters":P({"name":{"type":"string","default":""}}, [])},
            {"name":"memory_wiki_add_evidence","description":"Attach evidence to a claim and refresh it.","parameters":P({"claim_id":{"type":"string"},"text":{"type":"string"},"kind":{"type":"string","enum":["support","refute","source","note"],"default":"support"},"source":{"type":"string","default":"tool"}}, ["claim_id","text"])},
            {"name":"memory_wiki_update_claim","description":"Patch claim fields: claim/topic/status/confidence/salience/freshness.","parameters":P({"claim_id":{"type":"string"},"claim":{"type":"string"},"topic":{"type":"string"},"status":{"type":"string","enum":["active","retired","superseded","uncertain"]},"confidence":{"type":"number"},"salience":{"type":"number"},"refresh":{"type":"boolean","default":False}}, ["claim_id"])},
            {"name":"memory_wiki_contradict","description":"Record contradiction between two claims.","parameters":P({"claim_a":{"type":"string"},"claim_b":{"type":"string"},"reason":{"type":"string"}}, ["claim_a","claim_b","reason"])},
            {"name":"memory_wiki_resolve_contradiction","description":"Resolve a contradiction and optionally retire/supersede a claim.","parameters":P({"contradiction_id":{"type":"string"},"resolution":{"type":"string"},"winner_claim_id":{"type":"string"},"loser_status":{"type":"string","enum":["retired","superseded","uncertain"],"default":"superseded"}}, ["contradiction_id","resolution"])},
            {"name":"memory_wiki_dashboard","description":"Return dashboard: counts, topics, stale claims, contradictions, paths.","parameters":P({"limit":{"type":"integer","default":20}})},
            {"name":"memory_wiki_get_page","description":"Read a topic page from the markdown wiki vault.","parameters":P({"topic":{"type":"string"}}, ["topic"])},
            {"name":"memory_wiki_maintenance","description":"Run vault maintenance: rebuild FTS, detect contradictions, render pages, optionally prune low-salience retired claims.","parameters":P({"prune_retired_days":{"type":"integer","default":0}})},
            {"name":"memory_wiki_merge_claims","description":"Merge duplicate/overlapping claims, keeping one canonical claim and superseding or retiring the rest.","parameters":P({"keep_id":{"type":"string"},"merge_ids":{"type":"array","items":{"type":"string"}},"resolution":{"type":"string","default":"merged as duplicate"},"loser_status":{"type":"string","enum":["retired","superseded","uncertain"],"default":"superseded"}}, ["keep_id","merge_ids"])},
            {"name":"memory_wiki_import","description":"Import claims/evidence from a memory_wiki_export JSON payload.","parameters":P({"payload":{"type":"object"},"mode":{"type":"string","enum":["upsert"],"default":"upsert"}}, ["payload"])},
            {"name":"memory_wiki_curate","description":"Suggest or apply cleanup: bad topics, low-quality fragments, duplicate-like claims, and optional pinning.","parameters":P({"mode":{"type":"string","enum":["suggest","apply"],"default":"suggest"},"limit":{"type":"integer","default":80},"aggressiveness":{"type":"number","default":0.45}})},
            {"name":"memory_wiki_pin_claim","description":"Pin a claim so recall/curation preserve it.","parameters":P({"claim_id":{"type":"string"},"pinned":{"type":"boolean","default":True}}, ["claim_id"])},
            {"name":"memory_wiki_health","description":"Audit memory-wiki quality: schema, low-quality fragments, bad topics, raw logs, secret exposure, duplicate-like claims.","parameters":P({"limit":{"type":"integer","default":100}})},
            {"name":"memory_wiki_evaluate_retrieval","description":"Run a golden-query retrieval quality suite for recall/pack_context precision and artifact leakage.","parameters":P({"limit":{"type":"integer","default":10},"max_chars":{"type":"integer","default":3800}})},
            {"name":"memory_wiki_rewrite_claim","description":"Rewrite one claim in-place with normalized text/topic/type and quality recomputation.","parameters":P({"claim_id":{"type":"string"},"claim":{"type":"string"},"topic":{"type":"string"},"reason":{"type":"string","default":"manual rewrite"}}, ["claim_id","claim"])},
            {"name":"memory_wiki_explain_recall","description":"Explain why query returns specific claims.","parameters":P({"query":{"type":"string"},"limit":{"type":"integer","default":10},"topic":{"type":"string"}}, ["query"])},
            {"name":"memory_wiki_vacuum","description":"Aggressively deduplicate, merge near-duplicates, resolve stale contradiction noise, normalize topics, and optionally apply changes.","parameters":P({"mode":{"type":"string","enum":["suggest","apply"],"default":"suggest"},"limit":{"type":"integer","default":120},"similarity":{"type":"number","default":0.82},"max_pairs":{"type":"integer","default":2500}})},
            {"name":"memory_wiki_review_queue","description":"List/approve/reject/rewrite candidate memories before they become durable claims.","parameters":P({"mode":{"type":"string","enum":["list","approve","reject","rewrite"],"default":"list"},"item_id":{"type":"string"},"claim":{"type":"string"},"topic":{"type":"string"},"reason":{"type":"string"},"limit":{"type":"integer","default":20}})},
            {"name":"memory_wiki_lint_claim","description":"Lint a candidate memory claim and suggest topic/type/scope rewrites.","parameters":P({"claim":{"type":"string"},"topic":{"type":"string","default":"general"}}, ["claim"])},
            {"name":"memory_wiki_why_believe","description":"Explain provenance, evidence, trust score and contradictions for a claim.","parameters":P({"claim_id":{"type":"string"}}, ["claim_id"])},
            {"name":"memory_wiki_secret_quarantine","description":"List quarantined secret-like memory fields; originals are never returned, only hashes/redacted text.","parameters":P({"limit":{"type":"integer","default":20},"status":{"type":"string","default":"active"}})},
            {"name":"memory_wiki_recent_changes","description":"Show memory mutations since N seconds ago.","parameters":P({"since_seconds":{"type":"integer","default":3600},"limit":{"type":"integer","default":50}})},
            {"name":"memory_wiki_mark_used","description":"Feedback loop: mark recalled claims as useful or not useful.","parameters":P({"claim_ids":{"type":"array","items":{"type":"string"}},"usefulness":{"type":"number","default":1.0},"query":{"type":"string"}}, ["claim_ids"])},
            {"name":"memory_wiki_normalize_topics","description":"Suggest/apply topic alias normalization.","parameters":P({"mode":{"type":"string","enum":["suggest","apply"],"default":"suggest"},"limit":{"type":"integer","default":100}})},
            {"name":"memory_wiki_immune_scan","description":"Memory immune system: detect/apply fixes for secrets, raw blobs, bad topics, low-quality claims.","parameters":P({"mode":{"type":"string","enum":["suggest","apply"],"default":"suggest"},"limit":{"type":"integer","default":100}})},
            {"name":"memory_wiki_compress_topic","description":"Create a synthetic summary claim for a topic and optionally supersede older low-priority claims.","parameters":P({"topic":{"type":"string"},"mode":{"type":"string","enum":["suggest","apply"],"default":"suggest"},"limit":{"type":"integer","default":30}}, ["topic"])},
            {"name":"memory_wiki_resolve_by_policy","description":"Resolve contradiction using policy: prefer_explicit_user, prefer_recent, prefer_verified, prefer_environment_probe.","parameters":P({"contradiction_id":{"type":"string"},"policy":{"type":"string","default":"prefer_explicit_user"}}, ["contradiction_id"])},
            {"name":"memory_wiki_repair","description":"Run targeted self-healing repairs: fts, dashboards, integrity, or all.","parameters":P({"target":{"type":"string","enum":["fts","dashboards","integrity","all"],"default":"all"},"dry_run":{"type":"boolean","default":True}})},
            {"name":"memory_wiki_audit_log","description":"Show recent memory-wiki write/repair/backup/restore audit events.","parameters":P({"limit":{"type":"integer","default":50}})},
            {"name":"memory_wiki_write_firewall","description":"Dry-run or queue a candidate memory through source policy, quality lint, artifact detection and secret firewall before durable write.","parameters":P({"claim":{"type":"string"},"topic":{"type":"string","default":"general"},"evidence":{"type":"string","default":""},"source":{"type":"string","default":"tool"},"mode":{"type":"string","enum":["check","queue","apply"],"default":"check"},"confidence":{"type":"number","default":0.75},"salience":{"type":"number","default":0.7}}, ["claim"])},
            {"name":"memory_wiki_mutation_log","description":"Return transactional mutation log entries with before/after metadata for undo/audit.","parameters":P({"limit":{"type":"integer","default":50},"target_table":{"type":"string","default":""},"target_id":{"type":"string","default":""},"since_seconds":{"type":"integer","default":0}}, [])},
            {"name":"memory_wiki_undo_last","description":"Undo the last reversible memory mutation or a specific mutation id.","parameters":P({"mutation_id":{"type":"string","default":""},"dry_run":{"type":"boolean","default":True}}, [])},
            {"name":"memory_wiki_transaction","description":"Dry-run/apply a bounded batch of memory operations with optional pre-apply backup and rollback ids.","parameters":P({"operations":{"type":"array","items":{"type":"object"}},"mode":{"type":"string","enum":["suggest","apply","apply_with_backup"],"default":"suggest"},"reason":{"type":"string","default":""}}, ["operations"])},
            {"name":"memory_wiki_compile_topic","description":"Compile micro-claims in a topic into a curated structured summary; suggest or apply with superseding of older low-priority claims.","parameters":P({"topic":{"type":"string"},"mode":{"type":"string","enum":["suggest","apply"],"default":"suggest"},"limit":{"type":"integer","default":50},"summary_type":{"type":"string","enum":["summary","runbook","profile","timeline","decision"],"default":"summary"}}, ["topic"])},
            {"name":"memory_wiki_get_project_context","description":"Return first-class project profile plus related claims/task capsules/graph context.","parameters":P({"project_id":{"type":"string"},"query":{"type":"string","default":""},"limit":{"type":"integer","default":20}}, ["project_id"])},
            {"name":"memory_wiki_source_policy","description":"Show ingestion policy and write-firewall decision for a source/candidate.","parameters":P({"source":{"type":"string","default":"tool"},"claim":{"type":"string","default":""},"topic":{"type":"string","default":"general"}}, [])},
            {"name":"memory_wiki_export_bundle","description":"Export a redacted scoped sync bundle for cross-profile/remote memory-wiki import.","parameters":P({"topic":{"type":"string","default":""},"project_id":{"type":"string","default":""},"scope":{"type":"string","default":""},"limit":{"type":"integer","default":500},"write_file":{"type":"boolean","default":True}}, [])},
            {"name":"memory_wiki_import_bundle","description":"Import a redacted memory-wiki sync bundle from payload or local path.","parameters":P({"payload":{"type":"object"},"path":{"type":"string","default":""},"mode":{"type":"string","enum":["suggest","upsert"],"default":"suggest"}}, [])},
            {"name":"memory_wiki_export","description":"Export bounded claims/evidence/contradictions JSON.","parameters":P({"limit":{"type":"integer","default":200}})},
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        a = dict(args or {})
        _retry_after_reconnect = bool(a.pop("__retry_after_reconnect", False))
        if self._conn is None:
            self._connect(); self._migrate()
        try:
            if tool_name == "memory_wiki_query":
                rows = self._search(a.get("query",""), int(a.get("limit",10)), bool(a.get("include_stale",True)), a.get("topic"))
                return tool_result(success=True, claims=[self._rowdict(r) for r in rows])
            if tool_name == "memory_wiki_add_claim":
                cid = self._add_claim(a.get("claim",""), a.get("topic") or "general", a.get("evidence") or "", a.get("source") or "tool", float(a.get("confidence",.75)), float(a.get("salience",.7)))
                return tool_result(success=True, id=cid, page=str(self._topic_page(a.get("topic") or "general")))
            if tool_name == "memory_wiki_add_secret": return tool_result(success=True, **self._add_secret(a))
            if tool_name == "memory_wiki_query_secrets": return tool_result(success=True, secrets=self._query_secrets(a.get("query") or "", int(a.get("limit",10)), bool(a.get("reveal", False))))
            if tool_name == "memory_wiki_recall_plan": return tool_result(success=True, **self._recall_plan(a.get("query") or "", int(a.get("limit",8))))
            if tool_name == "memory_wiki_post_task": return tool_result(success=True, **self._post_task(a))
            if tool_name == "memory_wiki_active_dashboard": return tool_result(success=True, **self._active_dashboard(int(a.get("limit",80))))
            if tool_name == "memory_wiki_doctor": return tool_result(success=True, **self._doctor(bool(a.get("repair", False))))
            if tool_name == "memory_wiki_backup": return tool_result(success=True, **self._backup(a.get("reason") or "manual"))
            if tool_name == "memory_wiki_list_backups": return tool_result(success=True, backups=self._list_backups(int(a.get("limit",20))))
            if tool_name == "memory_wiki_restore": return tool_result(success=True, **self._restore(a.get("backup") or ""))
            if tool_name == "memory_wiki_add_decision": return tool_result(success=True, **self._add_decision(a))
            if tool_name == "memory_wiki_add_mistake": return tool_result(success=True, **self._add_mistake(a))
            if tool_name == "memory_wiki_add_project_profile": return tool_result(success=True, **self._add_project_profile(a))
            if tool_name == "memory_wiki_add_task_capsule": return tool_result(success=True, **self._add_task_capsule(a))
            if tool_name == "memory_wiki_add_entity": return tool_result(success=True, **self._add_entity(a))
            if tool_name == "memory_wiki_add_relation": return tool_result(success=True, **self._add_relation(a))
            if tool_name == "memory_wiki_graph_query": return tool_result(success=True, **self._graph_query(a.get("query") or "", int(a.get("limit",20))))
            if tool_name == "memory_wiki_apply_user_correction": return tool_result(success=True, **self._apply_user_correction(a))
            if tool_name == "memory_wiki_pack_context": return tool_result(success=True, **self._pack_context(a.get("query") or "", int(a.get("max_chars",MAX_PREFETCH_CHARS))))
            if tool_name == "memory_wiki_migrate_secrets_from_claims": return tool_result(success=True, **self._migrate_secrets_from_claims(bool(a.get("apply", True)), int(a.get("limit",100))))
            if tool_name == "memory_wiki_scrub_secrets": return tool_result(success=True, **self._scrub_secrets(bool(a.get("apply", not bool(a.get("dry_run", True)))), int(a.get("limit",200))))
            if tool_name == "memory_wiki_snapshot": return tool_result(success=True, **self._snapshot(a.get("name") or ""))
            if tool_name == "memory_wiki_add_evidence": return tool_result(success=True, id=self._add_evidence(a.get("claim_id",""), a.get("text",""), a.get("kind") or "support", a.get("source") or "tool"))
            if tool_name == "memory_wiki_update_claim": return tool_result(success=True, **self._update_claim(a))
            if tool_name == "memory_wiki_contradict": return tool_result(success=True, id=self._add_contradiction(a.get("claim_a",""), a.get("claim_b",""), a.get("reason","")))
            if tool_name == "memory_wiki_resolve_contradiction": return tool_result(success=True, **self._resolve_contradiction(a))
            if tool_name == "memory_wiki_dashboard": return tool_result(self._dashboard(int(a.get("limit",20))))
            if tool_name == "memory_wiki_get_page":
                p = self._topic_page(a.get("topic",""));
                if not p.exists(): self._render_topic(a.get("topic",""))
                return tool_result(success=p.exists(), path=str(p), content=p.read_text(encoding="utf-8") if p.exists() else "")
            if tool_name == "memory_wiki_maintenance":
                rep = self._maintenance(int(a.get("prune_retired_days",0) or 0)); return tool_result(success=True, **rep)
            if tool_name == "memory_wiki_merge_claims": return tool_result(success=True, **self._merge_claims(a))
            if tool_name == "memory_wiki_import": return tool_result(success=True, **self._import(a.get("payload") or {}))
            if tool_name == "memory_wiki_curate": return tool_result(success=True, **self._curate(a.get("mode") or "suggest", int(a.get("limit",80)), float(a.get("aggressiveness",.45))))
            if tool_name == "memory_wiki_pin_claim": return tool_result(success=True, **self._pin_claim(a.get("claim_id") or "", bool(a.get("pinned", True))))
            if tool_name == "memory_wiki_health": return tool_result(success=True, **self._health(int(a.get("limit",100))))
            if tool_name == "memory_wiki_evaluate_retrieval": return tool_result(success=True, **self._evaluate_retrieval(int(a.get("limit",10)), int(a.get("max_chars",3800))))
            if tool_name == "memory_wiki_rewrite_claim": return tool_result(success=True, **self._rewrite_claim(a))
            if tool_name == "memory_wiki_explain_recall": return tool_result(success=True, explanations=self._explain_recall(a.get("query",""), int(a.get("limit",10)), a.get("topic")))
            if tool_name == "memory_wiki_vacuum": return tool_result(success=True, **self._vacuum(a.get("mode") or "suggest", int(a.get("limit",120)), float(a.get("similarity",.82)), int(a.get("max_pairs",2500))))
            if tool_name == "memory_wiki_review_queue": return tool_result(success=True, **self._review_queue(a.get("mode") or "list", a.get("item_id") or "", a.get("claim") or "", a.get("topic") or "", a.get("reason") or "", int(a.get("limit",20))))
            if tool_name == "memory_wiki_lint_claim": return tool_result(success=True, **self._lint_claim(a.get("claim") or "", a.get("topic") or "general"))
            if tool_name == "memory_wiki_why_believe": return tool_result(success=True, **self._why_believe(a.get("claim_id") or ""))
            if tool_name == "memory_wiki_secret_quarantine": return tool_result(success=True, items=[self._sanitize_row(r) for r in self._connect().execute("SELECT * FROM secret_quarantine WHERE status=? ORDER BY created_at DESC LIMIT ?", (a.get("status") or "active", max(1,min(int(a.get("limit",20)),200)))).fetchall()])
            if tool_name == "memory_wiki_recent_changes": return tool_result(success=True, **self._recent_changes(int(a.get("since_seconds",3600)), int(a.get("limit",50))))
            if tool_name == "memory_wiki_mark_used": return tool_result(success=True, **self._mark_used(a.get("claim_ids") or [], float(a.get("usefulness",1.0)), a.get("query") or ""))
            if tool_name == "memory_wiki_normalize_topics": return tool_result(success=True, **self._normalize_topics(a.get("mode") or "suggest", int(a.get("limit",100))))
            if tool_name == "memory_wiki_immune_scan": return tool_result(success=True, **self._immune_scan(a.get("mode") or "suggest", int(a.get("limit",100))))
            if tool_name == "memory_wiki_compress_topic": return tool_result(success=True, **self._compress_topic(a.get("topic") or "general", a.get("mode") or "suggest", int(a.get("limit",30))))
            if tool_name == "memory_wiki_resolve_by_policy": return tool_result(success=True, **self._resolve_by_policy(a.get("contradiction_id") or "", a.get("policy") or "prefer_explicit_user"))
            if tool_name == "memory_wiki_repair": return tool_result(success=True, **self._repair(a.get("target") or "all", bool(a.get("dry_run", True))))
            if tool_name == "memory_wiki_audit_log": return tool_result(success=True, events=self._audit_log(int(a.get("limit",50))))
            if tool_name == "memory_wiki_write_firewall": return tool_result(success=True, **self._write_firewall(a))
            if tool_name == "memory_wiki_mutation_log": return tool_result(success=True, **self._mutation_log(int(a.get("limit",50)), a.get("target_table") or "", a.get("target_id") or "", int(a.get("since_seconds",0) or 0)))
            if tool_name == "memory_wiki_undo_last": return tool_result(success=True, **self._undo_last(a.get("mutation_id") or "", bool(a.get("dry_run", True))))
            if tool_name == "memory_wiki_transaction": return tool_result(success=True, **self._transaction(a.get("operations") or [], a.get("mode") or "suggest", a.get("reason") or ""))
            if tool_name == "memory_wiki_compile_topic": return tool_result(success=True, **self._compile_topic(a.get("topic") or "general", a.get("mode") or "suggest", int(a.get("limit",50)), a.get("summary_type") or "summary"))
            if tool_name == "memory_wiki_get_project_context": return tool_result(success=True, **self._get_project_context(a.get("project_id") or "", a.get("query") or "", int(a.get("limit",20))))
            if tool_name == "memory_wiki_source_policy": return tool_result(success=True, **self._source_policy_tool(a.get("source") or "tool", a.get("claim") or "", a.get("topic") or "general"))
            if tool_name == "memory_wiki_export_bundle": return tool_result(success=True, **self._export_bundle(a))
            if tool_name == "memory_wiki_import_bundle": return tool_result(success=True, **self._import_bundle(a))
            if tool_name == "memory_wiki_export": return tool_result(self._export(int(a.get("limit",200))))
            return tool_error(f"unknown memory-wiki tool: {tool_name}")
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "disk I/O error" in msg and not _retry_after_reconnect:
                try:
                    if self._conn is not None:
                        self._conn.close()
                except Exception:
                    pass
                self._conn = None
                retry_args = dict(a)
                retry_args["__retry_after_reconnect"] = True
                return self.handle_tool_call(tool_name, retry_args, **kwargs)
            if "disk I/O error" in msg:
                try:
                    spool_path = self._spool_event('tool_error', {'tool':tool_name, 'arguments':a, 'error':msg})
                    return tool_error(f"{msg}; operation spooled for manual replay: {spool_path}")
                except Exception:
                    pass
            return tool_error(msg)
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if not _retry_after_reconnect:
                try:
                    if self._conn is not None:
                        self._conn.close()
                except Exception:
                    pass
                self._conn = None
                retry_args = dict(a)
                retry_args["__retry_after_reconnect"] = True
                return self.handle_tool_call(tool_name, retry_args, **kwargs)
            try:
                self._preserve_db_files("database_error")
            except Exception:
                pass
            return tool_error(f"{msg}; provider connection/database error after reconnect. Run direct quick_check and restart Hermes/plugin runtime if the DB is valid.")
        except Exception as e:
            return tool_error(str(e))

    # ----- db ------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.root.mkdir(parents=True, exist_ok=True)
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            self.recovery_dir.mkdir(parents=True, exist_ok=True)
            last_exc: Optional[Exception] = None
            for attempt in range(3):
                conn: Optional[sqlite3.Connection] = None
                try:
                    conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA busy_timeout=30000")
                    conn.execute("PRAGMA foreign_keys=ON")
                    conn.execute("PRAGMA temp_store=MEMORY")
                    conn.execute("PRAGMA synchronous=FULL")
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                    except sqlite3.OperationalError as e:
                        if "disk I/O error" not in str(e).lower():
                            raise
                        conn.execute("PRAGMA journal_mode=DELETE")
                    conn.execute("SELECT 1").fetchone()
                    self._conn = conn
                    self._degraded = False
                    self._last_io_error = ""
                    break
                except sqlite3.OperationalError as e:
                    last_exc = e
                    self._last_io_error = str(e)
                    try:
                        if conn is not None:
                            conn.close()
                    except Exception:
                        pass
                    if "disk I/O error" in str(e).lower() and attempt == 0:
                        self._preserve_db_files("connect_io_error")
                    time.sleep(0.05 * (attempt + 1))
            if self._conn is None:
                self._degraded = True
                raise sqlite3.OperationalError(f"memory-wiki database unavailable after reconnect attempts: {last_exc}")
        return self._conn

    def _preserve_db_files(self, reason: str = "io_error") -> List[str]:
        """Best-effort copy of SQLite files for forensics before repair/restore attempts."""
        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y%m%d_%H%M%S', time.localtime(now()))
        copied: List[str] = []
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.db_path) + suffix)
            if not src.exists() or not src.is_file():
                continue
            dest = self.recovery_dir / f"{stamp}_{reason}_{src.name}"
            try:
                shutil.copy2(src, dest)
                copied.append(str(dest))
            except Exception:
                pass
        return copied

    def _spool_event(self, op: str, payload: Dict[str, Any]) -> str:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        ts = now()
        sid = "spool_" + sha(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + f":{ts}")[:16]
        path = self.spool_dir / f"{sid}.json"
        atomic_write(path, json.dumps({"id":sid,"op":op,"payload":payload,"created_at":ts,"last_io_error":self._last_io_error}, ensure_ascii=False, indent=2) + "\n")
        return str(path)

    def _checkpoint_wal(self, mode: str = "FULL") -> str:
        c = self._connect()
        try:
            row = c.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
            return str(tuple(row)) if row is not None else "ok"
        except sqlite3.OperationalError as e:
            if "disk I/O error" in str(e).lower() and mode.upper() != "PASSIVE":
                row = c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                return "passive " + (str(tuple(row)) if row is not None else "ok")
            raise

    def _migrate(self) -> None:
        c = self._connect()
        with c:
            c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            c.execute("""CREATE TABLE IF NOT EXISTS claims(
                id TEXT PRIMARY KEY, claim TEXT NOT NULL, topic TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                confidence REAL NOT NULL DEFAULT .70, salience REAL NOT NULL DEFAULT .70, source TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, freshness_at INTEGER NOT NULL, access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed INTEGER NOT NULL DEFAULT 0, hash TEXT NOT NULL UNIQUE)""")
            for col, typ, default in [("salience","REAL","0.70"),("access_count","INTEGER","0"),("last_accessed","INTEGER","0"),("quality","REAL","0.50"),("pinned","INTEGER","0"),("normalized_claim","TEXT","''"),("type","TEXT","'fact'"),("source_type","TEXT","'unknown'"),("last_verified_at","INTEGER","0"),("verification_status","TEXT","'unverified'"),("scope","TEXT","'global'"),("project_id","TEXT","''"),("usefulness","REAL","0.50"),("recall_count","INTEGER","0"),("last_recalled","INTEGER","0"),("trust_class","TEXT","'fact'"),("trust_score","REAL","0.55"),("risk","TEXT","'low'"),("custody","TEXT","'{}'"),("quarantined_at","INTEGER","0"),("quality_flags","TEXT","'[]'"),("source_ref","TEXT","''"),("derived_from","TEXT","''"),("review_state","TEXT","'accepted'")]:
                if col not in self._cols("claims"): c.execute(f"ALTER TABLE claims ADD COLUMN {col} {typ} NOT NULL DEFAULT {default}")
            if "normalized_claim" in self._cols("claims"):
                c.execute("UPDATE claims SET normalized_claim=claim WHERE normalized_claim='' OR normalized_claim IS NULL")
            if {"quality","type","source_type"}.issubset(self._cols("claims")):
                for r in c.execute("SELECT id,claim,topic,source FROM claims WHERE quality IS NULL OR quality=0 OR type='fact' OR source_type='unknown'").fetchall():
                    c.execute("UPDATE claims SET quality=?, type=?, source_type=? WHERE id=?", (claim_quality(r["claim"], r["topic"]), infer_claim_type(r["claim"], r["topic"]), infer_source_type(r["source"]), r["id"]))
            c.execute("""CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'support', text TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, FOREIGN KEY(claim_id) REFERENCES claims(id) ON DELETE CASCADE)""")
            c.execute("""CREATE TABLE IF NOT EXISTS contradictions(id TEXT PRIMARY KEY, claim_a TEXT NOT NULL, claim_b TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', resolution TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, resolved_at INTEGER, FOREIGN KEY(claim_a) REFERENCES claims(id) ON DELETE CASCADE, FOREIGN KEY(claim_b) REFERENCES claims(id) ON DELETE CASCADE)""")
            if "resolution" not in self._cols("contradictions"): c.execute("ALTER TABLE contradictions ADD COLUMN resolution TEXT NOT NULL DEFAULT ''")
            if "severity" not in self._cols("contradictions"): c.execute("ALTER TABLE contradictions ADD COLUMN severity TEXT NOT NULL DEFAULT 'possible'")
            c.execute("CREATE INDEX IF NOT EXISTS idx_claims_topic ON claims(topic)"); c.execute("CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status)"); c.execute("CREATE INDEX IF NOT EXISTS idx_claims_fresh ON claims(freshness_at)"); c.execute("CREATE INDEX IF NOT EXISTS idx_claims_scope_project ON claims(scope, project_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_claims_recall_scope ON claims(status, scope, project_id, topic, risk, trust_score, updated_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_claims_priority ON claims(status, pinned, salience, usefulness, trust_score, freshness_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_claims_type_topic ON claims(status, type, topic, updated_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_claims_hash_norm ON claims(hash, normalized_claim)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_claims_quality_flags ON claims(status, quality, trust_class, review_state)")
            c.execute("""CREATE TABLE IF NOT EXISTS review_queue(
                id TEXT PRIMARY KEY, candidate TEXT NOT NULL, topic TEXT NOT NULL DEFAULT 'general', source TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '', suggested_claim TEXT NOT NULL DEFAULT '', suggested_topic TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT .5,
                salience REAL NOT NULL DEFAULT .5, status TEXT NOT NULL DEFAULT 'pending', claim_id TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status, updated_at)")
            c.execute("""CREATE TABLE IF NOT EXISTS memory_changes(
                id TEXT PRIMARY KEY, action TEXT NOT NULL, claim_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_memory_changes_created ON memory_changes(created_at)")
            c.execute("""CREATE TABLE IF NOT EXISTS memory_mutations(
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT 'memory-wiki', operation TEXT NOT NULL,
                target_table TEXT NOT NULL DEFAULT '', target_id TEXT NOT NULL DEFAULT '', before_json TEXT NOT NULL DEFAULT '', after_json TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '', reversible INTEGER NOT NULL DEFAULT 1, undone_at INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_memory_mutations_target ON memory_mutations(target_table,target_id,created_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_memory_mutations_batch ON memory_mutations(batch_id,created_at)")
            c.execute("""CREATE TABLE IF NOT EXISTS source_policies(
                source_type TEXT PRIMARY KEY, policy_json TEXT NOT NULL, updated_at INTEGER NOT NULL)""")
            for st, pol in sorted(SOURCE_POLICY.items()):
                c.execute("INSERT OR REPLACE INTO source_policies(source_type,policy_json,updated_at) VALUES(?,?,?)", (st, json.dumps(pol, ensure_ascii=False, sort_keys=True), now()))
            c.execute("""CREATE TABLE IF NOT EXISTS sync_bundles(
                id TEXT PRIMARY KEY, path TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', payload_hash TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL DEFAULT 'export', created_at INTEGER NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sync_bundles_created ON sync_bundles(created_at)")
            c.execute("""CREATE TABLE IF NOT EXISTS audit_log(id TEXT PRIMARY KEY, op TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at)")
            c.execute("""CREATE TABLE IF NOT EXISTS recall_events(
                id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, query TEXT NOT NULL DEFAULT '', score REAL NOT NULL DEFAULT 0, used REAL NOT NULL DEFAULT -1, created_at INTEGER NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_recall_events_claim ON recall_events(claim_id, created_at)")
            c.execute("CREATE TABLE IF NOT EXISTS topic_aliases(alias TEXT PRIMARY KEY, topic TEXT NOT NULL)")
            for a,t in sorted(TOPIC_ALIASES.items()): c.execute("INSERT OR IGNORE INTO topic_aliases(alias,topic) VALUES(?,?)", (a,t))
            c.execute("""CREATE TABLE IF NOT EXISTS source_artifacts(
                id TEXT PRIMARY KEY, source_table TEXT NOT NULL DEFAULT 'claims', source_id TEXT NOT NULL, artifact_type TEXT NOT NULL,
                redacted_excerpt TEXT NOT NULL DEFAULT '', source_ref TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'archived',
                created_at INTEGER NOT NULL, hash TEXT NOT NULL UNIQUE)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_source_artifacts_source ON source_artifacts(source_table, source_id, artifact_type)")
            c.execute("""CREATE TABLE IF NOT EXISTS retrieval_eval_cases(
                id TEXT PRIMARY KEY, query TEXT NOT NULL, must_topics TEXT NOT NULL DEFAULT '[]', must_not_topics TEXT NOT NULL DEFAULT '[]',
                must_include TEXT NOT NULL DEFAULT '[]', must_not_include TEXT NOT NULL DEFAULT '[]', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_retrieval_eval_cases_updated ON retrieval_eval_cases(updated_at)")
            default_cases = [
                ('eval_memory_quality','правила качества memory-wiki',['memory-wiki','hermes'],[],['raw logs','structured claims'],['Traceback','[TOOL]']),
                ('eval_hermes_plugin','как патчить Hermes plugin',['hermes','memory-wiki'],[],['py_compile','smoke'],['full output:','stdout']),
                ('eval_secrets','как хранить секреты',['secrets'],[],['secret_index','redacted'],['password=','token=']),
                ('eval_preferences','что пользователь предпочитает в ответах',['preferences'],[],['русском','конкретику'],['Imported TencentDB','Traceback']),
                ('eval_android','как работать с Android/proot Hermes',['android','hermes'],[],['Android','proot'],['raw preview','[TOOL]']),
            ]
            for eid,q,mt,mnt,mi,mni in default_cases:
                c.execute("INSERT OR IGNORE INTO retrieval_eval_cases(id,query,must_topics,must_not_topics,must_include,must_not_include,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (eid,q,json.dumps(mt,ensure_ascii=False),json.dumps(mnt,ensure_ascii=False),json.dumps(mi,ensure_ascii=False),json.dumps(mni,ensure_ascii=False),now(),now()))
            c.execute("""CREATE TABLE IF NOT EXISTS secret_index(
                id TEXT PRIMARY KEY, subject TEXT NOT NULL, scope TEXT NOT NULL, secret_type TEXT NOT NULL DEFAULT 'credential',
                locator TEXT NOT NULL DEFAULT '', value TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT .85, salience REAL NOT NULL DEFAULT .85, status TEXT NOT NULL DEFAULT 'active',
                last_verified_at INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, hash TEXT NOT NULL UNIQUE)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_secret_index_subject ON secret_index(subject, scope, status)")
            c.execute("""CREATE TABLE IF NOT EXISTS secret_quarantine(
                id TEXT PRIMARY KEY, table_name TEXT NOT NULL, row_id TEXT NOT NULL, field TEXT NOT NULL,
                redacted_value TEXT NOT NULL DEFAULT '', original_hash TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active', created_at INTEGER NOT NULL, resolved_at INTEGER NOT NULL DEFAULT 0,
                UNIQUE(table_name,row_id,field,original_hash))""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_secret_quarantine_status ON secret_quarantine(status, created_at)")
            c.execute("""CREATE TABLE IF NOT EXISTS post_task_log(
                id TEXT PRIMARY KEY, summary TEXT NOT NULL, topic TEXT NOT NULL DEFAULT 'operations', changed_files TEXT NOT NULL DEFAULT '[]',
                backups TEXT NOT NULL DEFAULT '[]', verification TEXT NOT NULL DEFAULT '', services TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'post_task', created_at INTEGER NOT NULL)""")
            for col, typ, default in [("memory_role","TEXT","'environment'"),("type","TEXT","'task'")]:
                if col not in self._cols("post_task_log"): c.execute(f"ALTER TABLE post_task_log ADD COLUMN {col} {typ} NOT NULL DEFAULT {default}")
            c.execute("""CREATE TABLE IF NOT EXISTS backups(id TEXT PRIMARY KEY, path TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, decision TEXT NOT NULL, rationale TEXT NOT NULL DEFAULT '', topic TEXT NOT NULL DEFAULT 'decisions', alternatives TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL DEFAULT 'tool', created_at INTEGER NOT NULL, hash TEXT NOT NULL UNIQUE)""")
            c.execute("""CREATE TABLE IF NOT EXISTS mistakes(id TEXT PRIMARY KEY, trigger TEXT NOT NULL, mistake TEXT NOT NULL, fix TEXT NOT NULL DEFAULT '', prevention TEXT NOT NULL DEFAULT '', topic TEXT NOT NULL DEFAULT 'lessons', created_at INTEGER NOT NULL, hash TEXT NOT NULL UNIQUE)""")
            c.execute("""CREATE TABLE IF NOT EXISTS project_profiles(project_id TEXT PRIMARY KEY, root TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL DEFAULT '', commands TEXT NOT NULL DEFAULT '[]', services TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL)""")
            for col, typ, default in [("stack_json","TEXT","'{}'"),("current_status","TEXT","''"),("last_verified_at","INTEGER","0"),("scope","TEXT","'project'"),("source","TEXT","'project_profile'")]:
                if col not in self._cols("project_profiles"):
                    c.execute(f"ALTER TABLE project_profiles ADD COLUMN {col} {typ} NOT NULL DEFAULT {default}")
            c.execute("""CREATE TABLE IF NOT EXISTS task_capsules(id TEXT PRIMARY KEY, intent TEXT NOT NULL, topic TEXT NOT NULL DEFAULT 'tasks', plan TEXT NOT NULL DEFAULT '', files TEXT NOT NULL DEFAULT '[]', commands TEXT NOT NULL DEFAULT '[]', errors TEXT NOT NULL DEFAULT '[]', fixes TEXT NOT NULL DEFAULT '[]', verification TEXT NOT NULL DEFAULT '', followups TEXT NOT NULL DEFAULT '[]', created_at INTEGER NOT NULL, hash TEXT NOT NULL UNIQUE)""")
            c.execute("""CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY, name TEXT NOT NULL, entity_type TEXT NOT NULL DEFAULT 'thing', aliases TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL, hash TEXT NOT NULL UNIQUE)""")
            c.execute("""CREATE TABLE IF NOT EXISTS relations(id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL, confidence REAL NOT NULL DEFAULT .8, evidence TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, hash TEXT NOT NULL UNIQUE)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(subject,predicate)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_relations_object ON relations(object,predicate)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name, entity_type)")
            try:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(id UNINDEXED, claim, normalized, topic, evidence, search_text, tokenize='unicode61')")
                cols = self._cols("claims_fts")
                if "search_text" not in cols or "normalized" not in cols:
                    c.execute("DROP TABLE IF EXISTS claims_fts")
                    c.execute("CREATE VIRTUAL TABLE claims_fts USING fts5(id UNINDEXED, claim, normalized, topic, evidence, search_text, tokenize='unicode61')")
            except Exception:
                pass

    def _cols(self, table: str) -> set[str]: return {r[1] for r in self._connect().execute(f"PRAGMA table_info({table})").fetchall()}
    def _rowdict(self, r: sqlite3.Row) -> Dict[str, Any]:
        """Public row serialization: always apply the same redaction guard as why/export paths."""
        return self._sanitize_row(r)

    def _add_change(self, action: str, claim_id: str = "", detail: str = "") -> None:
        try:
            c=self._connect(); ts=now(); cid="chg_"+sha(f"{action}:{claim_id}:{detail}:{ts}")[:12]
            with c: c.execute("INSERT OR IGNORE INTO memory_changes(id,action,claim_id,detail,created_at) VALUES(?,?,?,?,?)", (cid, action, claim_id or "", short(redact_secrets(detail),1200), ts))
        except Exception: pass

    def _audit(self, op: str, status: str = "ok", detail: str = "") -> None:
        try:
            c=self._connect(); ts=now(); aid="aud_"+sha(f"{op}:{status}:{detail}:{ts}")[:14]
            with c: c.execute("INSERT OR IGNORE INTO audit_log(id,op,status,detail,created_at) VALUES(?,?,?,?,?)", (aid, short(op,120), short(status,40), short(redact_secrets(detail),1600), ts))
        except Exception: pass

    def _record_mutation(self, operation: str, target_table: str = "", target_id: str = "", before: Any = None, after: Any = None, reason: str = "", batch_id: str = "", reversible: bool = True) -> str:
        """Append-only mutation ledger. Values are redacted before storage; raw secrets never enter the ledger."""
        ts = now()
        before_json = redact_secrets(json.dumps(before or {}, ensure_ascii=False, sort_keys=True, default=str))
        after_json = redact_secrets(json.dumps(after or {}, ensure_ascii=False, sort_keys=True, default=str))
        mid = "mut_" + sha(f"{operation}:{target_table}:{target_id}:{before_json}:{after_json}:{ts}")[:14]
        with self._connect() as c:
            c.execute(
                """INSERT OR IGNORE INTO memory_mutations(id,batch_id,actor,operation,target_table,target_id,before_json,after_json,reason,reversible,undone_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, short(batch_id,80), "memory-wiki", short(operation,120), short(target_table,80), short(target_id,160), short(before_json,9000), short(after_json,9000), short(redact_secrets(reason),1200), 1 if reversible else 0, 0, ts),
            )
        return mid

    def _table_row(self, table: str, row_id: str, pk: str = "id") -> Dict[str, Any]:
        if not table or not row_id:
            return {}
        allowed = {"claims":"id", "evidence":"id", "review_queue":"id", "secret_index":"id", "post_task_log":"id", "decisions":"id", "mistakes":"id", "project_profiles":"project_id", "task_capsules":"id", "entities":"id", "relations":"id"}
        pk = allowed.get(table, pk)
        if table not in allowed:
            return {}
        row = self._connect().execute(f"SELECT * FROM {table} WHERE {pk}=?", (row_id,)).fetchone()
        return self._sanitize_row(row) if row else {}

    def _mutation_log(self, limit:int=50, target_table:str="", target_id:str="", since_seconds:int=0)->Dict[str,Any]:
        c=self._connect(); limit=max(1,min(int(limit or 50),500)); where=[]; params=[]
        if target_table:
            where.append("target_table=?"); params.append(target_table)
        if target_id:
            where.append("target_id=?"); params.append(target_id)
        if since_seconds:
            where.append("created_at>=?"); params.append(now()-max(1,int(since_seconds)))
        sql="SELECT * FROM memory_mutations" + (" WHERE "+" AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return {"events":[self._sanitize_row(r) for r in c.execute(sql, params).fetchall()]}

    def _restore_row_from_json(self, table: str, row_id: str, before_json: str) -> Dict[str,Any]:
        allowed = {"claims":"id", "project_profiles":"project_id", "entities":"id", "relations":"id", "task_capsules":"id", "post_task_log":"id", "decisions":"id", "mistakes":"id"}
        pk = allowed.get(table)
        if not pk:
            return {"restored":False,"reason":"table not reversible"}
        before = json.loads(before_json or "{}") if before_json else {}
        c=self._connect()
        with c:
            if not before:
                c.execute(f"DELETE FROM {table} WHERE {pk}=?", (row_id,))
                if table == "claims":
                    try: c.execute("DELETE FROM claims_fts WHERE id=?", (row_id,))
                    except Exception: pass
                return {"restored":True,"action":"delete_inserted"}
            cols=[k for k in before.keys() if k != pk]
            if c.execute(f"SELECT 1 FROM {table} WHERE {pk}=?", (row_id,)).fetchone():
                sets=", ".join(f"{k}=?" for k in cols)
                c.execute(f"UPDATE {table} SET {sets} WHERE {pk}=?", [before[k] for k in cols]+[row_id])
            else:
                keys=[pk]+cols
                vals=[before.get(pk,row_id)]+[before[k] for k in cols]
                c.execute(f"INSERT OR REPLACE INTO {table}({','.join(keys)}) VALUES({','.join('?' for _ in keys)})", vals)
        if table == "claims":
            self._upsert_fts(row_id)
        self._render_dashboards()
        return {"restored":True,"action":"restore_before"}

    def _undo_last(self, mutation_id: str = "", dry_run: bool = True) -> Dict[str,Any]:
        c=self._connect()
        if mutation_id:
            row=c.execute("SELECT * FROM memory_mutations WHERE id=?", (mutation_id,)).fetchone()
        else:
            row=c.execute("SELECT * FROM memory_mutations WHERE reversible=1 AND undone_at=0 ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            return {"found":False,"dry_run":dry_run}
        event=self._sanitize_row(row)
        result={"found":True,"dry_run":dry_run,"event":event,"would_restore":bool(row["before_json"]),"would_delete_inserted":not bool(row["before_json"])}
        if dry_run:
            return result
        restored=self._restore_row_from_json(row["target_table"], row["target_id"], row["before_json"])
        with c:
            c.execute("UPDATE memory_mutations SET undone_at=? WHERE id=?", (now(), row["id"]))
        self._record_mutation("undo", row["target_table"], row["target_id"], event, restored, f"undo {row['id']}", reversible=False)
        result.update(restored); return result

    def _write_firewall(self, a: Dict[str,Any]) -> Dict[str,Any]:
        claim_raw=str(a.get("claim") or "")
        evidence_raw=str(a.get("evidence") or "")
        source=str(a.get("source") or "tool")
        topic=a.get("topic") or self._infer_topic(claim_raw)
        scan=secret_scan(claim_raw + "\n" + evidence_raw)
        clean_claim=normalize_claim(scrub_memory_artifacts(claim_raw))
        lint=lint_claim_text(clean_claim, topic)
        policy=source_policy_for(source)
        gate=memory_gate_decision(clean_claim, topic, source)
        mode=(a.get("mode") or "check").lower()
        out={"mode":mode,"policy":policy,"secret_scan":{"raw_secret":scan.get("raw_secret"),"mentions_secret":scan.get("mentions_secret"),"redaction_markers":scan.get("redaction_markers"),"risk":scan.get("risk"),"findings":scan.get("findings",[])},"lint":lint,"gate":gate,"normalized":clean_claim,"suggested_topic":self._topic_alias(topic, clean_claim)}
        if mode == "queue" or (mode == "apply" and gate.get("action") == "queue"):
            out["review_id"] = self._enqueue_review(clean_claim, topic, evidence_raw, source, str(gate.get("reason") or "manual queue"), float(a.get("confidence",.75)), float(a.get("salience",.7)))
        elif mode == "apply" and gate.get("action") in ("accept", "redact"):
            out["claim_id"] = self._add_claim(clean_claim, topic, evidence_raw, source, float(a.get("confidence",.75)), float(a.get("salience",.7)))
        return out

    def _source_policy_tool(self, source: str = "tool", claim: str = "", topic: str = "general") -> Dict[str,Any]:
        policy=source_policy_for(source)
        out={"source":source,"policy":policy}
        if claim:
            out["firewall"] = self._write_firewall({"claim":claim,"topic":topic,"source":source,"mode":"check"})
        return out

    def _sanitize_row(self, row: Dict[str, Any] | sqlite3.Row) -> Dict[str, Any]:
        """Last-mile guard: public recall/export/tool rows must never expose raw secrets."""
        d = dict(row)
        for k, v in list(d.items()):
            if isinstance(v, str):
                d[k] = redact_secrets(v)
        if "value" in d:
            d["has_value"] = bool(d.get("value"))
            d["value"] = "<redacted>" if d["has_value"] else ""
        return d

    def _make_secret_index_from_raw(self, table: str, row_id: str, field: str, original: str, redacted: str) -> str:
        scan = secret_scan(original)
        first = (scan.get("findings") or [{}])[0]
        typ = slug(first.get("field") or first.get("kind") or "credential")
        locator = f"memory-wiki://{table}/{row_id}/{field}#sha256:{sha(original)[:16]}"
        return self._add_secret({
            "subject": f"memory-wiki scrubbed {table}.{field}",
            "scope": f"{table}:{row_id}",
            "secret_type": typ,
            "locator": locator,
            "value": "",
            "purpose": "Raw credential detected in memory and replaced with redaction marker; original value intentionally not stored in memory-wiki.",
            "source": "memory_wiki_scrub_secrets",
            "confidence": 0.72,
            "salience": 0.78,
        }).get("id", "")

    def _redact_with_secret_refs(self, table: str, row_id: str, field: str, original: str) -> Tuple[str, List[str]]:
        redacted = redact_secrets(original)
        if redacted == str(original or ""):
            return redacted, []
        sid = self._make_secret_index_from_raw(table, row_id, field, original, redacted)
        marker = f"[REDACTED_SECRET:{sid or 'unknown'}]"
        return redacted.replace("<REDACTED>", marker), ([sid] if sid else [])

    def _quarantine_secret(self, table: str, row_id: str, field: str, original: str, reason: str = "secret_scan") -> str:
        red = redact_secrets(original); qid = "sq_" + sha(f"{table}:{row_id}:{field}:{sha(original)}")[:12]; ts = now()
        with self._connect() as c:
            c.execute("""INSERT OR IGNORE INTO secret_quarantine(id,table_name,row_id,field,redacted_value,original_hash,reason,status,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?)""", (qid, table, row_id, field, short(red, 2000), sha(original), reason, "active", ts))
        self._audit("secret_quarantine", "ok", f"{table}.{field}:{row_id}:{reason}")
        return qid

    def _trust_meta(self, claim: str, topic: str, source: str, evidence: str = "") -> Dict[str, Any]:
        meta = memory_classify(f"{claim} {evidence}", topic, source)
        st = infer_source_type(source)
        trust = float(meta.get("trust", .55))
        if st == "explicit_user": trust = max(trust, .88)
        elif st == "tool": trust = max(trust, .72)
        elif st == "assistant": trust = min(trust, .45)
        risk = meta.get("risk", "low")
        if meta.get("secret_scan", {}).get("raw_secret"): risk = "secret"
        custody = {"source": short(source,180), "source_type": st, "captured_at": now(), "topic": topic, "risk": risk}
        return {"trust_class": meta.get("class","fact"), "trust_score": round(clamp(trust),3), "risk": risk, "custody": json.dumps(custody, ensure_ascii=False)}

    def _why_believe(self, claim_id: str) -> Dict[str, Any]:
        c=self._connect(); r=c.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not r: raise ValueError(f"claim not found: {claim_id}")
        ev=[self._sanitize_row(x) for x in c.execute("SELECT * FROM evidence WHERE claim_id=? ORDER BY created_at DESC LIMIT 12", (claim_id,)).fetchall()]
        cons=[self._sanitize_row(x) for x in c.execute("SELECT * FROM contradictions WHERE (claim_a=? OR claim_b=?) ORDER BY created_at DESC LIMIT 12", (claim_id,claim_id)).fetchall()]
        mutations=[self._sanitize_row(x) for x in c.execute("SELECT * FROM memory_mutations WHERE target_table='claims' AND target_id=? ORDER BY created_at DESC LIMIT 8", (claim_id,)).fetchall()]
        recalls=[self._sanitize_row(x) for x in c.execute("SELECT * FROM recall_events WHERE claim_id=? ORDER BY created_at DESC LIMIT 8", (claim_id,)).fetchall()]
        custody={}
        try: custody=json.loads(r["custody"] or "{}") if "custody" in r.keys() else {}
        except Exception: custody={}
        source_policy=source_policy_for(r["source"] if "source" in r.keys() else "")
        trust={
            "confidence": r["confidence"], "salience": r["salience"],
            "quality": r["quality"] if "quality" in r.keys() else claim_quality(r["claim"], r["topic"]),
            "trust_score": r["trust_score"] if "trust_score" in r.keys() else .55,
            "trust_class": r["trust_class"] if "trust_class" in r.keys() else memory_classify(r["claim"], r["topic"], r["source"]).get("class"),
            "risk": r["risk"] if "risk" in r.keys() else "low",
            "verification_status": r["verification_status"] if "verification_status" in r.keys() else "unverified",
            "source_type": r["source_type"] if "source_type" in r.keys() else infer_source_type(r["source"]),
            "source_policy": source_policy,
            "custody": custody,
            "evidence_count": len(ev),
            "contradiction_count": len(cons),
            "recall_count": r["recall_count"] if "recall_count" in r.keys() else 0,
            "last_verified_at": r["last_verified_at"] if "last_verified_at" in r.keys() else 0,
            "last_recalled": r["last_recalled"] if "last_recalled" in r.keys() else 0,
            "stale": self._is_stale(r["freshness_at"]),
        }
        return {"claim": self._sanitize_row(r), "evidence": ev, "contradictions": cons, "mutations": mutations, "recalls": recalls, "why": trust}

    def _topic_alias(self, topic: str, claim: str = "") -> str:
        t=canonical_topic(topic, claim); c=self._connect()
        try:
            row=c.execute("SELECT topic FROM topic_aliases WHERE alias=?", (slug(t),)).fetchone()
            if row: return slug(row["topic"])
        except Exception: pass
        return t

    def _enqueue_review(self, candidate: str, topic: str, evidence: str, source: str, reason: str, confidence=.5, salience=.5) -> str:
        cand=normalize_claim(candidate); lint=lint_claim_text(cand, topic); rid="rq_"+sha(cand.lower()+str(source))[:12]; ts=now()
        with self._connect() as c:
            c.execute("INSERT OR IGNORE INTO review_queue(id,candidate,topic,source,evidence,reason,suggested_claim,suggested_topic,confidence,salience,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (rid,cand,self._topic_alias(topic,cand),source,short(redact_secrets(evidence),2000),reason or '; '.join(lint['issues']),lint['normalized'],lint['topic'],clamp(confidence),clamp(salience),'pending',ts,ts))
        self._add_change('review_enqueue', rid, reason or cand); return rid

    def _review_queue(self, mode: str = "list", item_id: str = "", claim: str = "", topic: str = "", reason: str = "", limit: int = 20) -> Dict[str, Any]:
        mode=(mode or 'list').lower(); c=self._connect(); limit=max(1,min(int(limit or 20),100))
        if mode == 'list':
            rows=[dict(r) for r in c.execute("SELECT * FROM review_queue WHERE status='pending' ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()]
            return {"mode":mode,"pending":len(rows),"items":rows}
        row=c.execute("SELECT * FROM review_queue WHERE id=?", (item_id,)).fetchone()
        if not row: raise ValueError('review item not found')
        if mode in ('approve','rewrite'):
            final=normalize_claim(claim or row['suggested_claim'] or row['candidate']); t=self._topic_alias(topic or row['suggested_topic'] or row['topic'], final)
            cid=self._add_claim(final,t,row['evidence'],row['source'] or 'review_queue',float(row['confidence']),float(row['salience']))
            with c: c.execute("UPDATE review_queue SET status='approved', claim_id=?, updated_at=? WHERE id=?", (cid,now(),item_id))
            self._add_change('review_approve', cid, item_id); return {"mode":mode,"id":item_id,"claim_id":cid,"status":"approved"}
        if mode == 'reject':
            with c: c.execute("UPDATE review_queue SET status='rejected', reason=?, updated_at=? WHERE id=?", (reason or row['reason'],now(),item_id))
            self._add_change('review_reject', item_id, reason); return {"mode":mode,"id":item_id,"status":"rejected"}
        raise ValueError('mode must be list|approve|reject|rewrite')

    def _recent_changes(self, since_seconds: int = 3600, limit: int = 50) -> Dict[str, Any]:
        cutoff=now()-max(1,int(since_seconds or 3600)); limit=max(1,min(int(limit or 50),200)); c=self._connect()
        rows=[dict(r) for r in c.execute("SELECT * FROM memory_changes WHERE created_at>=? ORDER BY created_at DESC LIMIT ?", (cutoff,limit)).fetchall()]
        return {"since_seconds":since_seconds,"count":len(rows),"changes":rows}

    def _mark_used(self, claim_ids: List[str], usefulness: float = 1.0, query: str = "") -> Dict[str, Any]:
        u=clamp(float(usefulness)); ids=[str(x) for x in (claim_ids or []) if str(x)]; c=self._connect(); ts=now(); n=0
        with c:
            for cid in ids:
                cur = c.execute("UPDATE claims SET usefulness=(usefulness*0.75 + ?*0.25), updated_at=? WHERE id=?", (u,ts,cid)); n += int(cur.rowcount or 0)
                c.execute("UPDATE recall_events SET used=? WHERE claim_id=? AND used<0", (u,cid))
                self._add_evidence(cid, f"recall usefulness marked {u:.2f}: {short(query,180)}", "note", "memory_wiki_mark_used", commit=False)
        return {"updated":n,"usefulness":u}

    def _lint_claim(self, claim: str, topic: str = "") -> Dict[str, Any]: return lint_claim_text(claim, topic)

    def _normalize_topics(self, mode: str = "suggest", limit: int = 100) -> Dict[str, Any]:
        mode='apply' if mode=='apply' else 'suggest'; limit=max(1,min(int(limit or 100),1000)); c=self._connect(); fixes=[]
        rows=c.execute("SELECT id,claim,topic FROM claims WHERE status='active' ORDER BY updated_at DESC LIMIT ?", (limit*5,)).fetchall()
        for r in rows:
            nt=self._topic_alias(r['topic'], r['claim'])
            if nt != r['topic'] and len(fixes)<limit: fixes.append({"id":r['id'],"old_topic":r['topic'],"new_topic":nt,"claim":short(r['claim'],180)})
        applied=0
        if mode=='apply':
            with c:
                for f in fixes:
                    cur = c.execute("UPDATE claims SET topic=?, updated_at=? WHERE id=?", (f['new_topic'], now(), f['id']))
                    applied += int(cur.rowcount or 0)
                    self._add_change('topic_normalize', f['id'], f"{f['old_topic']} -> {f['new_topic']}")
            self._rebuild_fts(); self._render_all()
        return {"mode":mode,"fixes":fixes,"applied":applied}

    def _immune_scan(self, mode: str = "suggest", limit: int = 100) -> Dict[str, Any]:
        mode='apply' if mode=='apply' else 'suggest'; limit=max(1,min(int(limit or 100),500)); c=self._connect(); actions=[]
        for r in c.execute("SELECT * FROM claims WHERE status='active' ORDER BY updated_at DESC LIMIT 2000").fetchall():
            lint=lint_claim_text(r['claim'], r['topic']); act=''
            if 'contains secret-like material' in lint['issues']: act='retire_secret'
            elif lint['quality'] < .25 or 'raw blob/log; summarize first' in lint['issues']: act='queue_or_uncertain'
            elif r['topic'] != lint['topic']: act='retopic'
            if act and len(actions)<limit: actions.append({"action":act,"id":r['id'],"topic":r['topic'],"suggested_topic":lint['topic'],"issues":lint['issues'],"claim":short(redact_secrets(r['claim']),220)})
        applied={"retired":0,"uncertain":0,"retopic":0,"queued":0}
        if mode=='apply':
            with c:
                for a in actions:
                    if a['action']=='retire_secret': c.execute("UPDATE claims SET status='retired', updated_at=? WHERE id=?", (now(),a['id'])); applied['retired']+=1
                    elif a['action']=='retopic': c.execute("UPDATE claims SET topic=?, updated_at=? WHERE id=?", (a['suggested_topic'],now(),a['id'])); applied['retopic']+=1
                    else: c.execute("UPDATE claims SET status='uncertain', salience=salience*0.8, updated_at=? WHERE id=?", (now(),a['id'])); applied['uncertain']+=1
                    self._add_change('immune_'+a['action'], a['id'], ', '.join(a['issues']))
            self._rebuild_fts(); self._render_all()
        return {"mode":mode,"actions":actions,"applied":applied}

    def _compress_topic(self, topic: str, mode: str = "suggest", limit: int = 30) -> Dict[str, Any]:
        return self._compile_topic(topic, mode, limit, "summary")

    def _compile_topic(self, topic: str, mode: str = "suggest", limit: int = 50, summary_type: str = "summary") -> Dict[str, Any]:
        """Deterministic claim compiler: many microfacts -> one curated summary claim."""
        t=self._topic_alias(topic or 'general'); c=self._connect(); lim=max(5,min(int(limit or 50),160))
        rows=[dict(r) for r in c.execute("""SELECT * FROM claims
            WHERE topic=? AND status='active' AND id NOT LIKE 'c_summary_%' AND source NOT IN ('memory_wiki_compress_topic','memory_wiki_compile_topic')
            ORDER BY pinned DESC, salience DESC, confidence DESC, trust_score DESC, updated_at DESC LIMIT ?""", (t,lim)).fetchall()]
        rows=[r for r in rows if not is_ephemeral_fragment(r.get('claim','')) and str(r.get('risk','low'))!='secret' and int(r.get('quarantined_at') or 0)==0]
        if not rows: return {"topic":t,"summary":"","claim_id":"","superseded":0,"candidates":[]}
        by_type={}
        for r in rows:
            by_type.setdefault(str(r.get('type') or r.get('trust_class') or 'fact'), []).append(r)
        order=['preference','procedure','environment','decision','lesson','task_result','fact']
        lines=[]
        title={"summary":"summary","runbook":"runbook","profile":"profile","timeline":"timeline","decision":"decision log"}.get(summary_type, 'summary')
        lines.append(f"Compiled {title} for topic {t}:")
        for typ in order + sorted(k for k in by_type if k not in order):
            group=by_type.get(typ) or []
            if not group: continue
            best=[]
            for r in group[:8]:
                best.append(short(r['claim'], 180))
            lines.append(f"{typ}: " + "; ".join(best))
        summary=short(" ".join(lines), 1800)
        candidate_ids=[r['id'] for r in rows]
        result={"topic":t,"summary_type":summary_type,"summary":summary,"candidates":candidate_ids,"would_supersede":[],"mode":mode}
        for r in rows[8:]:
            if not int(r.get('pinned') or 0) and float(r.get('salience') or 0) < .92:
                result['would_supersede'].append(r['id'])
        if mode!='apply': return result
        cid=self._add_claim(summary,t,json.dumps({"compiled_ids":candidate_ids,"summary_type":summary_type},ensure_ascii=False),"memory_wiki_compile_topic",.90,.92)
        superseded=0
        with c:
            for rid in result['would_supersede']:
                before=self._table_row('claims', rid)
                c.execute("UPDATE claims SET status='superseded', derived_from=CASE WHEN derived_from='' THEN ? ELSE derived_from END, updated_at=? WHERE id=?", (cid, now(), rid))
                self._record_mutation('compile_topic_supersede', 'claims', rid, before, self._table_row('claims', rid), f"compiled into {cid}")
                superseded+=1
            try:
                c.execute("UPDATE claims SET type='procedure', trust_class='procedure', quality_flags=?, derived_from=? WHERE id=?", (json.dumps(['compiled_topic_summary'], ensure_ascii=False), json.dumps(candidate_ids, ensure_ascii=False), cid))
            except Exception:
                pass
        self._add_change('topic_compile', cid, f"{t}: superseded {superseded}"); self._rebuild_fts(); self._render_all(); result.update({"claim_id":cid,"superseded":superseded}); return result

    def _resolve_by_policy(self, contradiction_id: str, policy: str = "prefer_explicit_user") -> Dict[str, Any]:
        c=self._connect(); k=c.execute("SELECT * FROM contradictions WHERE id=?", (contradiction_id,)).fetchone()
        if not k: raise ValueError('contradiction not found')
        rows={r['id']:dict(r) for r in c.execute("SELECT * FROM claims WHERE id IN (?,?)", (k['claim_a'],k['claim_b'])).fetchall()}
        def score(r):
            s=float(r.get('confidence') or 0)+float(r.get('salience') or 0)+float(r.get('quality') or 0)+float(r.get('usefulness') or .5)
            src=str(r.get('source') or '')
            if policy=='prefer_explicit_user' and ('memory_tool' in src or 'turn:user' in src): s+=.7
            if policy=='prefer_recent': s+=math.exp(-age_days(r.get('updated_at') or 0)/30)
            if policy=='prefer_verified' and r.get('verification_status')=='verified': s+=.8
            if policy=='prefer_environment_probe' and r.get('type')=='environment': s+=.5
            return s
        winner=max(rows.values(), key=score); loser=[r for r in rows.values() if r['id']!=winner['id']][0]
        return self._resolve_contradiction({"contradiction_id":contradiction_id,"resolution":f"policy {policy} selected {winner['id']}","winner_claim_id":winner['id'],"loser_status":"uncertain"})

    # ----- claims --------------------------------------------------------

    def _add_claim(self, claim: str, topic="general", evidence="", source="tool", confidence=.7, salience=.7) -> str:
        raw_claim=scrub_memory_artifacts(str(claim or "")); raw_evidence=scrub_memory_artifacts(str(evidence or ""))
        raw_secret=bool(secret_scan(raw_claim + " " + raw_evidence).get("raw_secret"))
        if raw_secret:
            self._quarantine_secret("claims", "pending", "claim/evidence", raw_claim + "\n" + raw_evidence, "add_claim_raw_secret")
        claim = normalize_claim(redact_secrets(raw_claim)); evidence = short(redact_secrets(raw_evidence), 2000)
        if raw_secret:
            claim = REDACTION_TOKEN_RE.sub("[REDACTED_SECRET]", claim)
            evidence = REDACTION_TOKEN_RE.sub("[REDACTED_SECRET]", evidence)
        if not claim or is_ephemeral_fragment(claim): raise ValueError("empty or ephemeral claim")
        gate = memory_gate_decision(claim, topic, source)
        if gate.get("action") == "reject":
            raise ValueError("claim rejected by memory quality gate: " + str(gate.get("reason")))
        if gate.get("action") == "queue" and not str(source or "").startswith("phase6_curated_summary"):
            return self._enqueue_review(claim, topic or self._infer_topic(claim), evidence, source, str(gate.get("reason") or "quality gate"), confidence, salience)
        topic = self._topic_alias(topic or self._infer_topic(claim), claim); normalized = normalize_claim(claim); h = sha(normalized.lower()); cid = "c_" + h[:12]
        if raw_secret:
            sid = self._make_secret_index_from_raw("claims", cid, "claim/evidence", raw_claim + "\n" + raw_evidence, claim + "\n" + evidence)
            if sid and "[REDACTED_SECRET]" not in evidence:
                evidence = short(((evidence + "\n") if evidence else "") + "[REDACTED_SECRET]", 2000)
        ts = now(); quality = claim_quality(normalized, topic); pinned = 1 if PIN_MARKER in normalized.lower() or PIN_MARKER in str(evidence).lower() else 0; ctype = infer_claim_type(normalized, topic); stype = infer_source_type(source); scope=infer_scope(normalized, source, topic); project_id=current_project_id() if scope=="project" else ""; tm=self._trust_meta(normalized, topic, source, evidence)
        flags=[]
        if is_ephemeral_fragment(normalized): flags.append("raw_blob")
        if secret_scan(raw_claim + " " + raw_evidence).get("redaction_markers"): flags.append("redaction_marker")
        if topic in BAD_TOPICS or topic in FORBIDDEN_AUTO_TOPICS: flags.append("topic_uncertain")
        if quality < 0.35: flags.append("low_quality")
        source_ref = f"source:{short(source,120)}#sha256:{sha(raw_claim + raw_evidence)[:16]}"
        review_state = "queued" if flags and not pinned else "accepted"
        with self._lock:
            c = self._connect()
            with c:
                ex = c.execute("SELECT id FROM claims WHERE hash=?", (h,)).fetchone()
                if ex:
                    cid = ex["id"]
                    c.execute("UPDATE claims SET topic=?, source=?, source_type=?, type=?, normalized_claim=?, scope=?, project_id=?, evidence=CASE WHEN ?!='' THEN ? ELSE evidence END, confidence=max(confidence,?), salience=max(salience,?), quality=max(quality,?), pinned=max(pinned,?), trust_class=?, trust_score=max(trust_score,?), risk=?, custody=?, quality_flags=?, source_ref=CASE WHEN source_ref='' THEN ? ELSE source_ref END, review_state=?, quarantined_at=CASE WHEN ? THEN ? ELSE quarantined_at END, updated_at=?, freshness_at=? WHERE id=?", (topic, source, stype, ctype, normalized, scope, project_id, evidence, evidence, clamp(confidence), clamp(salience), quality, pinned, tm["trust_class"], tm["trust_score"], str(tm["risk"]), tm["custody"], json.dumps(flags,ensure_ascii=False), source_ref, review_state, 1 if str(tm["risk"])=="secret" else 0, ts, ts, ts, cid))
                else:
                    c.execute("INSERT INTO claims(id,claim,topic,status,confidence,salience,source,evidence,created_at,updated_at,freshness_at,hash,quality,pinned,normalized_claim,type,source_type,verification_status,last_verified_at,scope,project_id,usefulness,recall_count,last_recalled,trust_class,trust_score,risk,custody,quarantined_at,quality_flags,source_ref,derived_from,review_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (cid, claim, topic, "active", clamp(confidence), clamp(salience), redact_secrets(source), short(evidence,2000), ts, ts, ts, h, quality, pinned, normalized, ctype, stype, "unverified", 0, scope, project_id, .5, 0, 0, tm["trust_class"], tm["trust_score"], str(tm["risk"]), tm["custody"], ts if str(tm["risk"])=="secret" else 0, json.dumps(flags,ensure_ascii=False), source_ref, "", review_state))
                if evidence: self._add_evidence(cid, evidence, "support", source, commit=False)
                after_row = self._table_row("claims", cid)
                self._record_mutation("upsert_claim", "claims", cid, {} if not ex else {"id": cid, "note": "pre-existing claim updated"}, after_row, source)
            self._upsert_fts(cid); self._detect_contradictions_for(cid); self._add_change("upsert_claim", cid, claim); self._render_topic(topic); self._render_dashboards()
        return cid

    # ----- env/config metadata -------------------------------------------
    def _env_files(self) -> List[Path]:
        paths = [self.home / ".env", self.home / "proxy" / ".env"]
        return [p for p in paths if p.exists() and p.is_file()]

    def _parse_env_metadata(self, path: Path) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        section = ""
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return out
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                label = line.lstrip("#").strip(" -—")
                if label:
                    section = short(label, 80)
                continue
            m = ENV_ASSIGN_RE.match(raw)
            if not m:
                continue
            name = m.group(1)
            value = os.environ.get(name, "")
            out.append({"name": name, "section": section, "path": str(path), "state": _env_value_state(name, value)})
        return out

    def _sync_env_metadata(self) -> None:
        if self.agent_context not in ("primary", "foreground", ""):
            return
        metas: List[Dict[str, str]] = []
        for p in self._env_files():
            metas.extend(self._parse_env_metadata(p))
        if not metas:
            return
        by_section: Dict[str, List[str]] = {}
        sensitive: List[str] = []
        all_names: List[str] = []
        for m in metas:
            name = m["name"]
            all_names.append(name)
            by_section.setdefault(m.get("section") or "Other", []).append(name)
            if _looks_sensitive_env_name(name):
                sensitive.append(name)
        section_bits: List[str] = []
        for sec, names in sorted(by_section.items()):
            uniq = sorted(set(names))
            tail = "…" if len(uniq) > 10 else ""
            section_bits.append(f"{sec}({len(uniq)}): " + ", ".join(uniq[:10]) + tail)
        files = sorted(str(p) for p in self._env_files())
        claim = short(
            "Hermes env/config metadata summary (values redacted, generated live): "
            f"files={', '.join(files)}; variables={len(set(all_names))}; sensitive_names={len(set(sensitive))}; "
            "sections=" + " | ".join(section_bits[:6]),
            900,
        )
        evidence = json.dumps({"files": files, "sensitive_names": sorted(set(sensitive)), "variables": sorted(set(all_names))}, ensure_ascii=False)
        try:
            cid = self._add_claim(claim, "config", evidence, "memory-wiki:env-metadata:v2", 0.90, 0.84)
            with self._connect() as c:
                c.execute(
                    """UPDATE claims SET status='superseded', updated_at=?
                       WHERE status='active' AND id!=? AND (
                         source IN ('memory-wiki:env-metadata','memory-wiki:env-metadata:v2')
                         OR claim LIKE 'Hermes env/config metadata lists configured variables%'
                       )""",
                    (now(), cid),
                )
        except Exception:
            pass

    def _env_metadata_context(self, query: str) -> str:
        if not SECRET_META_QUERY_RE.search(query or ""):
            return ""
        metas: List[Dict[str, str]] = []
        for p in self._env_files():
            metas.extend(self._parse_env_metadata(p))
        if not metas:
            return ""
        qlow = (query or "").lower()
        filtered = [m for m in metas if m["name"].lower() in qlow or (m.get("section") or "").lower() in qlow]
        if not filtered:
            filtered = metas
        lines = ["\nEnv/config metadata (secret values are never shown):"]
        for m in filtered[:40]:
            sec = f" section={m['section']}" if m.get("section") else ""
            lines.append(f"- {m['name']}: {m['state']}{sec} file={m['path']}")
        return "\n".join(lines)

    def _add_evidence(self, claim_id: str, text: str, kind="support", source="tool", *, commit=True) -> str:
        if not claim_id or not text: raise ValueError("claim_id and text are required")
        text = short(redact_secrets(scrub_memory_artifacts(text)), 2500); source = short(redact_secrets(source), 300)
        if not text:
            text = "[context removed: system/tool artifact]"
        eid = "e_" + sha(f"{claim_id}:{kind}:{source}:{text}")[:12]; ts = now(); c = self._connect()
        def work():
            c.execute("INSERT OR IGNORE INTO evidence(id,claim_id,kind,text,source,created_at) VALUES(?,?,?,?,?,?)", (eid, claim_id, kind, text, source, ts))
            if kind in ("support","source"):
                c.execute("UPDATE claims SET freshness_at=?, updated_at=?, confidence=min(1.0, confidence+0.03), salience=min(1.0, salience+0.02) WHERE id=?", (ts, ts, claim_id))
            elif kind == "refute":
                c.execute("UPDATE claims SET updated_at=?, confidence=max(0.0, confidence-0.12), status=CASE WHEN confidence<0.35 THEN 'uncertain' ELSE status END WHERE id=?", (ts, claim_id))
        if commit:
            with c: work()
            self._upsert_fts(claim_id); self._render_all()
        else: work()
        return eid

    def _update_claim(self, a: Dict[str, Any]) -> Dict[str, Any]:
        cid = a.get("claim_id") or ""; c = self._connect(); row = c.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
        if not row: raise ValueError(f"claim not found: {cid}")
        fields=[]; vals=[]
        new_topic = self._topic_alias(a.get("topic") or row["topic"], a.get("claim") or row["claim"])
        for k in ("claim","topic","status"):
            if a.get(k) is not None:
                fields.append(f"{k}=?")
                if k == "topic": vals.append(self._topic_alias(a[k], a.get("claim") or row["claim"]))
                elif k == "status": vals.append(normalize_claim_status(a[k]))
                else: vals.append(normalize_claim(a[k]))
        if a.get("claim") is not None:
            new_claim = normalize_claim(a.get("claim"))
            fields.append("normalized_claim=?"); vals.append(new_claim)
            fields.append("hash=?"); vals.append(sha(new_claim.lower()))
            fields.append("quality=?"); vals.append(claim_quality(new_claim, new_topic))
            fields.append("type=?"); vals.append(infer_claim_type(new_claim, new_topic))
        for k in ("confidence","salience"):
            if a.get(k) is not None: fields.append(f"{k}=?"); vals.append(clamp(float(a[k])))
        if a.get("refresh"): fields.append("freshness_at=?"); vals.append(now())
        fields.append("updated_at=?"); vals.append(now()); vals.append(cid)
        before = self._sanitize_row(row)
        with c: c.execute(f"UPDATE claims SET {', '.join(fields)} WHERE id=?", vals)
        self._record_mutation("update_claim", "claims", cid, before, self._table_row("claims", cid), a.get("reason") or "memory_wiki_update_claim")
        self._upsert_fts(cid); self._render_all(); return {"id": cid, "updated": True}

    def _set_status_by_text(self, text: str, status: str, reason: str) -> None:
        h = sha(short(text,1400).lower()); c = self._connect(); row = c.execute("SELECT id FROM claims WHERE hash=?", (h,)).fetchone()
        if row:
            with c: c.execute("UPDATE claims SET status=?, updated_at=? WHERE id=?", (status, now(), row["id"])); self._add_evidence(row["id"], reason, "note", reason, commit=False)
            self._upsert_fts(row["id"]); self._render_all()

    # ----- search/scoring -----------------------------------------------

    def _search(self, query: str, limit=10, include_stale=True, topic: Optional[str]=None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 10), 50)); q = query or ""; qt = tokens(q); c = self._connect(); candidates: Dict[str, sqlite3.Row] = {}; bm25: Dict[str, float] = {}
        topic_slug = self._topic_alias(topic, "") if topic else ""; pid=current_project_id()
        strict = os.environ.get("MEMORY_WIKI_STRICT_RECALL", "1").lower() not in ("0", "false", "no")
        base_where = "status='active' AND (scope!='project' OR project_id=?)"
        if strict:
            base_where += " AND risk!='secret' AND quarantined_at=0 AND trust_class NOT IN ('tool_log','raw_blob','secret') AND type!='source_artifact' AND quality>=0.20"
        if topic_slug:
            base_where += " AND topic=?"
        base_params = [pid] + ([topic_slug] if topic_slug else [])
        def add_rows(sql: str, params: List[Any], cap: int = 80) -> None:
            try:
                for r in c.execute(sql + f" LIMIT {int(cap)}", params).fetchall(): candidates.setdefault(r["id"], r)
            except Exception: pass
        if q.strip():
            for ftsq in (safe_fts_query(q, mode="and"), safe_fts_query(q, mode="or")):
                try:
                    fts_sql = "SELECT claims.*, bm25(claims_fts) AS rank FROM claims_fts JOIN claims ON claims_fts.id=claims.id WHERE claims_fts MATCH ? AND claims.status='active'"
                    fts_params: List[Any] = [ftsq]
                    if strict:
                        fts_sql += " AND claims.risk!='secret' AND claims.quarantined_at=0 AND claims.trust_class NOT IN ('tool_log','raw_blob','secret') AND claims.type!='source_artifact' AND claims.quality>=0.20"
                    if topic_slug: fts_sql += " AND claims.topic=?"; fts_params.append(topic_slug)
                    fts_sql += " AND (claims.scope!='project' OR claims.project_id=?) ORDER BY rank"; fts_params.append(pid)
                    for r in c.execute(fts_sql + " LIMIT 100", fts_params).fetchall(): candidates.setdefault(r["id"], r); bm25[r["id"]] = max(bm25.get(r["id"], 0.0), bm25_norm(r["rank"]))
                except Exception: pass
            like = f"%{q.strip()[:180]}%"
            add_rows(f"SELECT * FROM claims WHERE {base_where} AND (claim LIKE ? OR normalized_claim LIKE ? OR evidence LIKE ?)", base_params + [like, like, like], 80)
        add_rows(f"SELECT * FROM claims WHERE {base_where} ORDER BY pinned DESC, salience DESC, usefulness DESC, trust_score DESC, updated_at DESC", base_params, 120)
        add_rows(f"SELECT * FROM claims WHERE {base_where} ORDER BY updated_at DESC", base_params, 160)
        add_rows(f"SELECT * FROM claims WHERE {base_where} AND risk!='secret' ORDER BY recall_count ASC, freshness_at DESC", base_params, 80)
        scored=[]
        for r in candidates.values():
            if r["status"] != "active": continue
            if str(r["risk"] if "risk" in r.keys() else "low") == "secret": continue
            if int(r["quarantined_at"] if "quarantined_at" in r.keys() else 0) > 0: continue
            quality = float(r["quality"] if "quality" in r.keys() else claim_quality(r["claim"], r["topic"]))
            trust_class = str(r["trust_class"] if "trust_class" in r.keys() else "fact")
            typ = str(r["type"] if "type" in r.keys() else "fact")
            pinned = int(r["pinned"] if "pinned" in r.keys() else 0)
            if strict and not pinned and (quality < 0.28 or trust_class in ("tool_log", "raw_blob", "secret") or typ == "source_artifact" or is_ephemeral_fragment(r["claim"])):
                continue
            stale = self._is_stale(r["freshness_at"])
            if stale and not include_stale: continue
            blob = claim_search_text(r["claim"], r["normalized_claim"] if "normalized_claim" in r.keys() else "", r["topic"], r["evidence"]); rt = tokens(blob)
            lexical = (len(qt & rt) / max(1, len(qt))) if qt else 0.15
            exact = 0.35 if q.lower() and q.lower() in blob.lower() else 0.0
            freshness = math.exp(-age_days(r["freshness_at"]) / 45.0); recency = math.exp(-age_days(r["updated_at"]) / 120.0)
            access = min(0.12, math.log1p(int(r["access_count"] or 0)) / 35.0)
            usefulness = float(r["usefulness"] if "usefulness" in r.keys() else .5)
            parts = score_breakdown(r, q, qt, lexical, exact, freshness, recency, access, quality, usefulness, pinned, stale)
            parts["bm25"] = bm25.get(r["id"], 0.0)
            score = sum(parts.values())
            d = self._sanitize_row(r); d["score"] = round(score, 4); d["score_parts"] = {k: round(v,4) for k,v in parts.items() if abs(v) > 0.0001}; scored.append(d)
        scored.sort(key=lambda x: x["score"], reverse=True)
        ids = [x["id"] for x in scored[:limit]]
        if ids:
            with c:
                ts=now(); c.executemany("UPDATE claims SET access_count=access_count+1, recall_count=recall_count+1, last_accessed=?, last_recalled=? WHERE id=?", [(ts, ts, i) for i in ids])
                c.executemany("INSERT OR IGNORE INTO recall_events(id,claim_id,query,score,used,created_at) VALUES(?,?,?,?,?,?)", [("re_"+sha(f"{i}:{q}:{ts}")[:12], i, short(q,500), next((float(x.get("score",0)) for x in scored if x["id"]==i),0.0), -1, ts) for i in ids])
        return scored[:limit]

    def _upsert_fts(self, cid: str) -> None:
        c = self._connect(); r = c.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
        if not r: return
        normalized = r["normalized_claim"] if "normalized_claim" in r.keys() else r["claim"]
        doc = claim_search_text(r["claim"], normalized, r["topic"], r["evidence"])
        try:
            with c:
                c.execute("DELETE FROM claims_fts WHERE id=?", (cid,)); c.execute("INSERT INTO claims_fts(id,claim,normalized,topic,evidence,search_text) VALUES(?,?,?,?,?,?)", (cid, r["claim"], normalized, r["topic"], r["evidence"], doc))
        except Exception:
            try:
                with c:
                    c.execute("DROP TABLE IF EXISTS claims_fts")
                    c.execute("CREATE VIRTUAL TABLE claims_fts USING fts5(id UNINDEXED, claim, normalized, topic, evidence, search_text, tokenize='unicode61')")
            except Exception: pass

    def _rebuild_fts(self) -> None:
        c = self._connect()
        try:
            with c:
                c.execute("DROP TABLE IF EXISTS claims_fts")
                c.execute("CREATE VIRTUAL TABLE claims_fts USING fts5(id UNINDEXED, claim, normalized, topic, evidence, search_text, tokenize='unicode61')")
                for r in c.execute("SELECT id,claim,normalized_claim,topic,evidence FROM claims").fetchall():
                    normalized = r["normalized_claim"] or r["claim"]
                    c.execute("INSERT INTO claims_fts(id,claim,normalized,topic,evidence,search_text) VALUES(?,?,?,?,?,?)", (r["id"],r["claim"],normalized,r["topic"],r["evidence"],claim_search_text(r["claim"], normalized, r["topic"], r["evidence"])))
        except Exception: pass

    # ----- extraction ----------------------------------------------------
    def _ingest_text(self, text: str, *, source: str, max_claims=8) -> None:
        text = scrub_memory_artifacts(str(text or "")).strip()
        if not text or len(text) < 25 or is_ephemeral_fragment(text): return
        src = str(source or "")
        is_user_turn = src.startswith("turn:user:")
        is_assistant_turn = src.startswith("turn:assistant:")
        explicit = extract_memory_directive(text) if is_user_turn else ""
        chunks: List[Tuple[int, str, bool]] = []
        if explicit and len(explicit) >= 18 and not is_ephemeral_fragment(explicit):
            chunks.append((5, explicit, True))
        corpus = explicit or text
        for raw in SENT_RE.split(corpus):
            s = normalize_claim(scrub_memory_artifacts(raw))
            if len(s) < 35 or len(s) > 700: continue
            if is_ephemeral_fragment(s): continue
            low = s.lower(); score = 0
            triggers = ["remember", "prefers", "preference", "correction", "environment", "config", "project", "uses ", "installed", "path", "port", "android", "termux", "hermes", "plugin", "memory", "ssh", "api", "token", "запомни", "предпочитает", "исправ", "проект", "использует", "установ", "андроид", "память", "плагин", "конфиг"]
            score += sum(1 for t in triggers if t in low)
            if PATH_RE.search(s) or URL_RE.search(s): score += 2
            if re.search(r"\b\d{2,5}\b", s): score += 1
            if "do not" in low or "never" in low or "никогда" in low or "нельзя" in low: score += 2
            if redact_secrets(s) != s: score -= 1
            if is_assistant_turn and not (PATH_RE.search(s) or URL_RE.search(s) or any(k in low for k in ("installed", "установ", "config", "конфиг", "port", "service", "systemd"))):
                score -= 2
            threshold = MIN_EXPLICIT_INGEST_SCORE if explicit and is_user_turn else MIN_AUTO_INGEST_SCORE
            if score >= threshold and claim_quality(s, self._infer_topic(s)) >= 0.32:
                chunks.append((score, s, False))
        chunks.sort(key=lambda x: (x[2], x[0]), reverse=True)
        seen=set()
        for score, s, was_explicit in chunks[:max_claims]:
            key=sha(s.lower())[:12]
            if key in seen: continue
            seen.add(key)
            self._add_claim(
                s,
                self._infer_topic(s),
                short(redact_secrets(scrub_memory_artifacts(corpus)),700),
                source,
                confidence=clamp((.72 if was_explicit else .55)+score*.05),
                salience=clamp((.72 if was_explicit else .50)+score*.06),
            )


    def _infer_topic(self, text: str) -> str:
        low=text.lower()
        rules=[
            ("preferences", ("пользователь предпочитает","user prefers","user workflow preferences","хочет, чтобы","нужно, чтобы","mobile-first","не соглашался автоматически")),
            ("memory-wiki", ("memory-wiki","memory_wiki","memory wiki","claim","recall","pack_context","memory database")),
            ("secrets", ("token","api key","apikey","secret","password","пароль","ключ","credential","secret_index")),
            ("android", ("android","termux","андроид","apk","oauth","proot")),
            ("hermes", ("hermes","plugin","tool registry","плагин","память")),
            ("telegram", ("telegram","bot token","tg_","чат","канал")),
            ("openclaw", ("openclaw","openclaw.json","/home/.openclaw")),
            ("server", ("vps","server","systemd","ssh","nginx","port","health","сервер","сервис")),
            ("proxy", ("proxy","прокси","gateway","deepseek")),
            ("api", ("api","openai","model","gpt","claude","endpoint")),
            ("database", ("sqlite","postgres","qdrant","database","db")),
            ("github", ("github","git","repo","pull request")),
            ("config", ("config.yaml",".env","конфиг","settings")),
        ]
        for topic, keys in rules:
            if any(k in low for k in keys): return topic
        m = re.search(r"project[:/\s-]+([a-zA-Z0-9_.-]{3,40})", text or "", re.I)
        if m: return "project-" + slug(m.group(1), 48)
        scored=[t for t in tokens(text) if len(t) >= 4 and not t.isdigit() and t not in BAD_TOPICS]
        first = scored[0] if scored else "general"
        return first if first not in FORBIDDEN_AUTO_TOPICS else "general"

    # ----- contradictions/maintenance -----------------------------------
    def _add_contradiction(self, a: str, b: str, reason: str) -> str:
        if not a or not b or a == b: raise ValueError("two distinct claim ids required")
        kid = "k_" + sha(":".join(sorted([a,b])) + reason)[:12]
        with self._connect() as c: c.execute("INSERT OR IGNORE INTO contradictions(id,claim_a,claim_b,reason,status,created_at) VALUES(?,?,?,?,?,?)", (kid,a,b,reason or "possible contradiction","open",now()))
        self._render_dashboards(); return kid


    def _detect_contradictions_for(self, cid: str) -> None:
        c=self._connect(); r=c.execute("SELECT * FROM claims WHERE id=?",(cid,)).fetchone()
        if not r or r["status"] != "active": return
        if is_ephemeral_fragment(r["claim"]) or float(r["quality"] or 0) < 0.45: return
        if str(r["type"] if "type" in r.keys() else "fact") in ("procedure", "task_result", "source_artifact"):
            return
        claim_low = str(r["claim"] or "").lower()
        source_s = str(r["source"] if "source" in r.keys() else "")
        if source_s.startswith("memory-wiki:env-metadata") or claim_low.startswith("hermes env/config metadata"):
            return
        t=tokens(r["claim"]); neg=self._neg(r["claim"])
        if not neg:
            return
        for o in c.execute("SELECT id,claim,topic,quality,type,scope FROM claims WHERE id!=? AND status='active' AND topic=? ORDER BY updated_at DESC LIMIT 120",(cid,r["topic"])).fetchall():
            if is_ephemeral_fragment(o["claim"]) or float(o["quality"] or 0) < 0.45: continue
            if str(o["type"] if "type" in o.keys() else "fact") in ("procedure", "task_result", "source_artifact"):
                continue
            other_low = str(o["claim"] or "").lower()
            if other_low.startswith("hermes env/config metadata"):
                continue
            if str(o["scope"] if "scope" in o.keys() else "global") != str(r["scope"] if "scope" in r.keys() else "global"):
                continue
            ot=tokens(o["claim"])
            overlap=len(t & ot); sim=overlap / max(1, len(t | ot))
            if overlap >= 18 and sim >= 0.42 and neg != self._neg(o["claim"]):
                self._add_contradiction(cid,o["id"],"high-overlap active same-topic same-scope factual claims with opposite negation markers")

    def _neg(self, s: str) -> bool:
        low=f" {s.lower()} "; return any(m in low for m in (" not "," never "," no ","n't"," do not ","не ","никогда","нельзя","без "))

    def _resolve_contradiction(self, a: Dict[str, Any]) -> Dict[str, Any]:
        kid=a.get("contradiction_id") or ""; res=a.get("resolution") or "resolved"; winner=a.get("winner_claim_id") or ""; loser_status=a.get("loser_status") or "superseded"; c=self._connect()
        row=c.execute("SELECT * FROM contradictions WHERE id=?",(kid,)).fetchone()
        if not row: raise ValueError(f"contradiction not found: {kid}")
        with c:
            c.execute("UPDATE contradictions SET status='resolved', resolution=?, resolved_at=? WHERE id=?",(res,now(),kid))
            if winner in (row["claim_a"],row["claim_b"]):
                loser=row["claim_b"] if winner==row["claim_a"] else row["claim_a"]
                c.execute("UPDATE claims SET status=?, updated_at=? WHERE id=?",(loser_status,now(),loser))
        self._render_all(); return {"id":kid,"resolved":True}

    def _merge_claims(self, a: Dict[str, Any]) -> Dict[str, Any]:
        keep = a.get("keep_id") or ""; merge_ids = [i for i in (a.get("merge_ids") or []) if i and i != keep]
        if not keep or not merge_ids: raise ValueError("keep_id and non-empty merge_ids are required")
        loser_status = a.get("loser_status") or "superseded"; res = a.get("resolution") or "merged as duplicate"; ts = now(); c = self._connect()
        keep_row = c.execute("SELECT * FROM claims WHERE id=?", (keep,)).fetchone()
        if not keep_row: raise ValueError(f"claim not found: {keep}")
        moved = 0
        with c:
            for mid in merge_ids:
                row = c.execute("SELECT * FROM claims WHERE id=?", (mid,)).fetchone()
                if not row: continue
                cur = c.execute("UPDATE evidence SET claim_id=? WHERE claim_id=?", (keep, mid)); moved += int(cur.rowcount or 0)
                note = f"{res}; merged `{mid}` into `{keep}`: {row['claim']}"
                c.execute("INSERT OR IGNORE INTO evidence(id,claim_id,kind,text,source,created_at) VALUES(?,?,?,?,?,?)", ("e_"+sha(f"{keep}:note:merge:{mid}:{note}")[:12], keep, "note", short(note,2500), "merge", ts))
                c.execute("UPDATE claims SET status=?, updated_at=? WHERE id=?", (loser_status, ts, mid))
                c.execute("UPDATE contradictions SET status='resolved', resolution=?, resolved_at=? WHERE status='open' AND (claim_a=? OR claim_b=?)", (res, ts, mid, mid))
        self._upsert_fts(keep)
        for mid in merge_ids: self._upsert_fts(mid)
        self._render_all(); return {"kept": keep, "merged": merge_ids, "evidence_moved": moved}


    # ----- curation/security ---------------------------------------------
    def _pin_claim(self, claim_id: str, pinned: bool = True) -> Dict[str, Any]:
        c = self._connect(); row = c.execute("SELECT id FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row: raise ValueError(f"claim not found: {claim_id}")
        with c: c.execute("UPDATE claims SET pinned=?, updated_at=? WHERE id=?", (1 if pinned else 0, now(), claim_id))
        self._render_all(); return {"id": claim_id, "pinned": bool(pinned)}

    def _curate(self, mode: str = "suggest", limit: int = 80, aggressiveness: float = .45) -> Dict[str, Any]:
        mode = mode if mode in ("suggest","apply") else "suggest"; limit=max(1,min(limit,300)); ag=clamp(aggressiveness)
        c=self._connect(); actions=[]; seen_by_sig={}
        rows=c.execute("SELECT * FROM claims ORDER BY updated_at DESC LIMIT ?", (max(limit*8, 200),)).fetchall()
        for r in rows:
            q=claim_quality(r["claim"], r["topic"]); new_topic=self._infer_topic(r["claim"])
            if int(r["pinned"] or 0):
                continue
            if (r["topic"] in BAD_TOPICS or str(r["topic"]).isdigit()) and new_topic != r["topic"]:
                actions.append({"action":"retopic", "id":r["id"], "from":r["topic"], "to":new_topic, "reason":"bad topic"})
            if is_ephemeral_fragment(r["claim"]):
                actions.append({"action":"retire_artifact", "id":r["id"], "reason":"system/tool artifact"})
            if q < max(.18, .38*ag) and float(r["salience"]) < .55:
                actions.append({"action":"mark_uncertain", "id":r["id"], "quality":q, "reason":"low quality fragment"})
            sig=" ".join(sorted(list(tokens(r["claim"]))[:8]))
            if sig and sig in seen_by_sig and len(tokens(r["claim"]) & tokens(seen_by_sig[sig]["claim"])) >= 5:
                keep = r["id"] if float(r["salience"])+float(r["confidence"]) > float(seen_by_sig[sig]["salience"])+float(seen_by_sig[sig]["confidence"]) else seen_by_sig[sig]["id"]
                lose = seen_by_sig[sig]["id"] if keep == r["id"] else r["id"]
                actions.append({"action":"merge_duplicate", "keep_id":keep, "merge_id":lose, "reason":"similar token signature"})
            else:
                seen_by_sig[sig]=r
            if len(actions) >= limit: break
        applied=[]
        if mode == "apply":
            with c:
                for a in actions:
                    if a["action"] == "retopic":
                        c.execute("UPDATE claims SET topic=?, quality=?, updated_at=? WHERE id=?", (a["to"], claim_quality(c.execute("SELECT claim FROM claims WHERE id=?",(a["id"],)).fetchone()["claim"], a["to"]), now(), a["id"])); applied.append(a)
                    elif a["action"] == "mark_uncertain":
                        c.execute("UPDATE claims SET status='uncertain', salience=max(0.0,salience-0.15), quality=?, updated_at=? WHERE id=?", (a["quality"], now(), a["id"])); applied.append(a)
                    elif a["action"] == "retire_artifact":
                        c.execute("UPDATE claims SET status='retired', salience=0.0, quality=0.0, updated_at=? WHERE id=?", (now(), a["id"])); applied.append(a)
                    elif a["action"] == "merge_duplicate":
                        c.execute("UPDATE claims SET status='superseded', updated_at=? WHERE id=?", (now(), a["merge_id"])); applied.append(a)
            self._rebuild_fts(); self._render_all()
        return {"mode":mode, "suggested":len(actions), "applied":len(applied), "actions":actions}

    def _maintenance(self, prune_retired_days: int = 0) -> Dict[str, Any]:
        self._rebuild_fts(); c=self._connect(); n=0
        for r in c.execute("SELECT id FROM claims WHERE status='active' ORDER BY updated_at DESC LIMIT 500").fetchall(): self._detect_contradictions_for(r["id"]); n+=1
        pruned=0
        if prune_retired_days > 0:
            cutoff=now()-prune_retired_days*86400
            with c:
                ids=[r["id"] for r in c.execute("SELECT id FROM claims WHERE status!='active' AND updated_at<? AND salience<0.3",(cutoff,)).fetchall()]
                for i in ids: c.execute("DELETE FROM claims WHERE id=?",(i,)); pruned+=1
        self._render_all(); return {"checked":n,"pruned":pruned,"dashboard":str(self.dashboard_dir/"index.md")}

    def _sim(self, a: Iterable[str], b: Iterable[str]) -> float:
        sa=set(a); sb=set(b)
        return len(sa & sb) / max(1, len(sa | sb))


    def _canonical_topic_for_claim(self, claim: str, topic: str) -> str:
        inferred = self._infer_topic(claim)
        t = slug(topic)
        if t in BAD_TOPICS or str(t).isdigit() or len(str(t or "")) < 3:
            return inferred
        if t in CANONICAL_TOPICS and t not in BAD_TOPICS:
            return t
        if inferred != "general" and (t in FORBIDDEN_AUTO_TOPICS or len(t) < 5):
            return inferred
        if t in FORBIDDEN_AUTO_TOPICS:
            return "general"
        return t

    def _vacuum(self, mode: str = "suggest", limit: int = 120, similarity: float = .82, max_pairs: int = 2500) -> Dict[str, Any]:
        mode = "apply" if mode == "apply" else "suggest"; limit=max(1,min(limit,1000)); similarity=max(.55,min(float(similarity),.98)); max_pairs=max(100,min(max_pairs,20000))
        c=self._connect(); rows=[self._rowdict(r) for r in c.execute("SELECT * FROM claims WHERE status='active' ORDER BY pinned DESC, salience DESC, confidence DESC, updated_at DESC LIMIT ?", (min(5000, max(limit*20, 500)),)).fetchall()]
        actions=[]; seen_pairs=0
        by_hash: Dict[str, List[Dict[str,Any]]] = {}
        for r in rows: by_hash.setdefault(sha(normalize_claim(r["claim"]).lower()), []).append(r)
        def keep_best(group):
            return sorted(group, key=lambda r:(int(r.get("pinned") or 0), float(r.get("salience") or 0), float(r.get("confidence") or 0), int(r.get("updated_at") or 0)), reverse=True)[0]
        for group in by_hash.values():
            if len(group) < 2: continue
            keep=keep_best(group)
            for r in group:
                if r["id"] != keep["id"] and len(actions) < limit: actions.append({"action":"merge_exact","keep_id":keep["id"],"merge_id":r["id"],"similarity":1.0,"reason":"identical normalized claim"})
        toks={r["id"]: tokens(r["claim"]) for r in rows}
        for i, a in enumerate(rows):
            if len(actions) >= limit: break
            for b in rows[i+1:]:
                seen_pairs += 1
                if seen_pairs > max_pairs: break
                if a["topic"] != b["topic"] and not (a["topic"] in BAD_TOPICS or b["topic"] in BAD_TOPICS): continue
                s=self._sim(toks[a["id"]], toks[b["id"]])
                if s >= similarity:
                    keep=keep_best([a,b]); loser=b if keep["id"]==a["id"] else a
                    if not any(x.get("merge_id")==loser["id"] for x in actions): actions.append({"action":"merge_near","keep_id":keep["id"],"merge_id":loser["id"],"similarity":round(s,3),"reason":"high token overlap"})
            if seen_pairs > max_pairs: break
        artifact_fixes=[]
        for r in rows:
            if is_ephemeral_fragment(r["claim"]) and len(artifact_fixes) < limit:
                artifact_fixes.append({"id":r["id"],"reason":"system/tool artifact","claim":short(r["claim"],180)})
        topic_fixes=[]
        for r in rows:
            nt=self._canonical_topic_for_claim(r["claim"], r["topic"])
            if nt != r["topic"] and len(topic_fixes) < limit:
                topic_fixes.append({"id":r["id"],"old_topic":r["topic"],"new_topic":nt,"claim":short(r["claim"],180)})
        stale_cons=[dict(r) for r in c.execute("SELECT * FROM contradictions WHERE status='open' AND (claim_a NOT IN (SELECT id FROM claims WHERE status='active') OR claim_b NOT IN (SELECT id FROM claims WHERE status='active')) LIMIT ?", (limit,)).fetchall()]
        applied={"merged":0,"topic_fixes":0,"resolved_contradictions":0,"retired_artifacts":0}
        if mode == "apply":
            with c:
                for a in artifact_fixes[:limit]:
                    c.execute("UPDATE claims SET status='retired', salience=0.0, quality=0.0, updated_at=? WHERE id=?", (now(), a["id"])); applied["retired_artifacts"]+=1
                for a in actions[:limit]:
                    row=c.execute("SELECT status FROM claims WHERE id=?",(a["merge_id"],)).fetchone()
                    if row and row["status"] == "active":
                        c.execute("UPDATE claims SET status='superseded', updated_at=? WHERE id=?", (now(), a["merge_id"])); self._add_evidence(a["keep_id"], f"vacuum merged {a['merge_id']}: {a['reason']} sim={a['similarity']}", "note", "memory_wiki_vacuum", commit=False); applied["merged"]+=1
                for f in topic_fixes:
                    before = c.total_changes
                    c.execute("UPDATE claims SET topic=?, updated_at=? WHERE id=? AND topic=?", (f["new_topic"], now(), f["id"], f["old_topic"]))
                    if c.total_changes > before: applied["topic_fixes"] += 1
                for k in stale_cons:
                    c.execute("UPDATE contradictions SET status='resolved', resolution=?, resolved_at=? WHERE id=?", ("vacuum: endpoint claim not active", now(), k["id"])); applied["resolved_contradictions"] += 1
            self._rebuild_fts(); self._render_all()
        return {"mode":mode,"similarity":similarity,"pairs_checked":min(seen_pairs,max_pairs),"suggestions":len(actions)+len(artifact_fixes),"actions":actions[:limit],"artifact_fixes":artifact_fixes,"topic_fixes":topic_fixes,"stale_contradictions":stale_cons,"applied":applied,"dashboard":str(self.dashboard_dir/"index.md")}

    def _import(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict): raise ValueError("payload must be an object from memory_wiki_export")
        c=self._connect(); ci=ei=ki=0
        with c:
            for r in payload.get("claims", []) or []:
                if not r.get("id") or not r.get("claim"): continue
                clean_claim=short(redact_secrets(r.get("claim","")),1400); clean_topic=self._topic_alias(r.get("topic") or self._infer_topic(clean_claim) or "general", clean_claim); clean_status=normalize_claim_status(r.get("status") or "active"); clean_ev=short(redact_secrets(r.get("evidence","")),2500)
                c.execute("""INSERT INTO claims(id,hash,claim,topic,evidence,status,confidence,salience,freshness_at,created_at,updated_at,access_count,last_accessed,quality,pinned)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(id) DO UPDATE SET claim=excluded.claim,topic=excluded.topic,evidence=excluded.evidence,status=excluded.status,confidence=excluded.confidence,salience=excluded.salience,freshness_at=excluded.freshness_at,updated_at=excluded.updated_at,quality=excluded.quality,pinned=max(pinned,excluded.pinned)""",
                          (r["id"], r.get("hash") or sha(clean_claim.lower()), clean_claim, clean_topic, clean_ev, clean_status, clamp(float(r.get("confidence",.75))), clamp(float(r.get("salience",.7))), int(r.get("freshness_at") or now()), int(r.get("created_at") or now()), int(r.get("updated_at") or now()), int(r.get("access_count") or 0), int(r.get("last_accessed") or 0), clamp(float(r.get("quality", claim_quality(clean_claim, clean_topic)))), int(r.get("pinned") or (PIN_MARKER in clean_claim.lower()) or 0)))
                ci+=1
            for e in payload.get("evidence", []) or []:
                if not e.get("claim_id") or not e.get("text"): continue
                eid=e.get("id") or "e_"+sha(f"{e.get('claim_id')}:{e.get('kind','support')}:{e.get('text')}")[:12]
                c.execute("INSERT OR IGNORE INTO evidence(id,claim_id,kind,text,source,created_at) VALUES(?,?,?,?,?,?)", (eid,e.get("claim_id"),e.get("kind") or "support",short(redact_secrets(e.get("text","")),2500),redact_secrets(e.get("source") or "import"),int(e.get("created_at") or now()))); ei+=1
            for k in payload.get("contradictions", []) or []:
                if not k.get("id") or not k.get("claim_a") or not k.get("claim_b"): continue
                c.execute("INSERT OR IGNORE INTO contradictions(id,claim_a,claim_b,reason,status,created_at,resolution,resolved_at) VALUES(?,?,?,?,?,?,?,?)", (k.get("id"),k.get("claim_a"),k.get("claim_b"),k.get("reason") or "imported contradiction",k.get("status") or "open",int(k.get("created_at") or now()),k.get("resolution"),k.get("resolved_at"))); ki+=1
        self._rebuild_fts(); self._render_all(); return {"claims":ci,"evidence":ei,"contradictions":ki,"dashboard":str(self.dashboard_dir/"index.md")}

    # ----- render/dashboard/export --------------------------------------
    def _is_stale(self, ts: int) -> bool: return age_days(ts) > STALE_DAYS
    def _topic_page(self, topic: str) -> Path: return self.pages_dir / f"{slug(topic)}.md"
    def _top_evidence(self, cid: str, limit=3) -> List[Dict[str,Any]]: return [dict(r) for r in self._connect().execute("SELECT * FROM evidence WHERE claim_id=? ORDER BY created_at DESC LIMIT ?",(cid,limit)).fetchall()]
    def _related_contradictions(self, ids: Iterable[str], limit=8) -> List[Dict[str,Any]]:
        ids=list(ids)
        if not ids: return []
        qs=",".join("?" for _ in ids)
        return [dict(r) for r in self._connect().execute(f"SELECT * FROM contradictions WHERE status='open' AND (claim_a IN ({qs}) OR claim_b IN ({qs})) ORDER BY created_at DESC LIMIT ?", ids+ids+[limit]).fetchall()]

    def _claim_refs(self, text: str) -> List[str]:
        return re.findall(r"`?(c_[a-f0-9]{12})`?", text or "")

    def _backlinks_for(self, ids: Iterable[str], limit=8) -> List[Dict[str,Any]]:
        ids=set(ids); out=[]
        if not ids: return out
        for r in self._connect().execute("SELECT id,topic,claim FROM claims WHERE status='active' ORDER BY updated_at DESC LIMIT 1000").fetchall():
            refs=set(self._claim_refs(r["claim"]))
            if refs & ids and r["id"] not in ids:
                out.append(dict(r))
                if len(out) >= limit: break
        return out

    def _render_topic(self, topic: str) -> None:
        topic=slug(topic); c=self._connect(); rows=c.execute("SELECT * FROM claims WHERE topic=? ORDER BY status, salience DESC, updated_at DESC LIMIT ?",(topic,MAX_RENDER_CLAIMS_PER_TOPIC)).fetchall()
        ids=[r["id"] for r in rows]
        total=c.execute("SELECT count(*) n FROM claims WHERE topic=?",(topic,)).fetchone()["n"]
        lines=[f"# {topic}","",f"Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}",""]
        if total > len(rows): lines.append(f"Showing top {len(rows)} of {total} claims.")
        lines += ["","## Claims"]
        for r in rows:
            flags=[]
            if int(r["pinned"] or 0): flags.append("pinned")
            if self._is_stale(r["freshness_at"]): flags.append("stale")
            lines.append(f"- `{r['id']}` **{r['status']}** conf={r['confidence']:.2f} sal={r['salience']:.2f} {' '.join('`'+f+'`' for f in flags)}: {r['claim']}")
            for e in self._top_evidence(r["id"],3): lines.append(f"  - {e['kind']} from {e['source']}: {short(e['text'],220)}")
        backlinks=self._backlinks_for(ids)
        if backlinks:
            lines += ["","## Backlinks"] + [f"- `{b['id']}` ({b['topic']}): {short(b['claim'],220)}" for b in backlinks]
        contr=self._related_contradictions(ids)
        if contr:
            lines += ["","## Open contradictions"] + [f"- `{k['id']}` {k['claim_a']} ↔ {k['claim_b']}: {k['reason']}" for k in contr]
        atomic_write(self._topic_page(topic), "\n".join(lines)+"\n")

    def _render_all(self) -> None:
        for r in self._connect().execute("SELECT DISTINCT topic FROM claims ORDER BY topic LIMIT ?",(MAX_RENDER_TOPICS,)).fetchall(): self._render_topic(r["topic"])
        self._render_dashboards()

    def _dashboard(self, limit=20) -> Dict[str,Any]:
        c=self._connect(); limit=max(1,min(limit,100))
        counts={r["status"]:r["n"] for r in c.execute("SELECT status,count(*) n FROM claims GROUP BY status").fetchall()}
        topics=[dict(r) for r in c.execute("SELECT topic,count(*) n,avg(confidence) confidence,avg(salience) salience FROM claims GROUP BY topic ORDER BY n DESC LIMIT ?",(limit,)).fetchall()]
        stale=[dict(r) for r in c.execute("SELECT id,topic,claim,freshness_at FROM claims WHERE status='active' ORDER BY freshness_at ASC LIMIT ?",(limit,)).fetchall() if self._is_stale(r["freshness_at"])]
        contr=[dict(r) for r in c.execute("SELECT * FROM contradictions WHERE status='open' ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()]
        top=[dict(r) for r in c.execute("SELECT id,topic,claim,confidence,salience,quality,pinned,access_count FROM claims WHERE status='active' ORDER BY salience DESC, confidence DESC LIMIT ?",(limit,)).fetchall()]
        has_review_queue = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_queue'").fetchone() is not None
        review_pending = c.execute("SELECT count(*) n FROM review_queue WHERE status='pending'").fetchone()["n"] if has_review_queue else 0
        return {"success":True,"version":"1.2.1","root":str(self.root),"db":str(self.db_path),"counts":counts,"topics":topics,"top_claims":top,"stale":stale,"contradictions":contr,"review_pending":review_pending,"dashboard":str(self.dashboard_dir/"index.md")}

    def _render_dashboards(self) -> None:
        d=self._dashboard(40); lines=["# Memory-Wiki Dashboard","",f"Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}","",f"Vault: `{self.root}`","","## Counts"]
        lines += [f"- {k}: {v}" for k,v in d["counts"].items()] or ["- empty"]
        lines += ["","## Topics"] + [f"- [[../pages/{slug(t['topic'])}.md|{t['topic']}]] — {t['n']} claims, conf={t['confidence']:.2f}, sal={t['salience']:.2f}" for t in d["topics"]]
        top_lines=[]
        for r in d["top_claims"][:15]:
            pin = '📌' if r.get('pinned') else ''
            top_lines.append(f"- `{r['id']}` ({r['topic']}) conf={r['confidence']:.2f} sal={r['salience']:.2f} q={r.get('quality',0):.2f} {pin} hits={r['access_count']}: {r['claim']}")
        lines += ["","## Top claims"] + top_lines
        lines += ["","## Open contradictions"] + ([f"- `{k['id']}` {k['claim_a']} ↔ {k['claim_b']}: {k['reason']}" for k in d["contradictions"]] or ["- none"])
        lines += ["","## Stale claims"] + ([f"- `{s['id']}` ({s['topic']}): {s['claim']}" for s in d["stale"]] or ["- none"])
        atomic_write(self.dashboard_dir/"index.md", "\n".join(lines)+"\n")

    def _rewrite_claim(self, a: Dict[str, Any]) -> Dict[str, Any]:
        cid = a.get("claim_id") or ""; new_claim = normalize_claim(a.get("claim") or ""); reason = a.get("reason") or "manual rewrite"
        if not cid or not new_claim: raise ValueError("claim_id and claim are required")
        c = self._connect(); row = c.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
        if not row: raise ValueError(f"claim not found: {cid}")
        topic = slug(a.get("topic") or row["topic"] or self._infer_topic(new_claim)); h = sha(new_claim.lower()); ts = now()
        before = self._sanitize_row(row)
        with c:
            c.execute("UPDATE claims SET claim=?, normalized_claim=?, hash=?, topic=?, type=?, quality=?, updated_at=? WHERE id=?", (new_claim, new_claim, h, topic, infer_claim_type(new_claim, topic), claim_quality(new_claim, topic), ts, cid))
            self._add_evidence(cid, f"rewrite: {reason}; old: {short(row['claim'],500)}", "note", "memory_wiki_rewrite_claim", commit=False)
        self._record_mutation("rewrite_claim", "claims", cid, before, self._table_row("claims", cid), reason)
        self._upsert_fts(cid); self._render_all()
        return {"id": cid, "topic": topic, "quality": claim_quality(new_claim, topic), "rewritten": True}


    def _health(self, limit: int = 100) -> Dict[str, Any]:
        c = self._connect(); limit=max(1,min(limit,1000)); cols=set(self._cols("claims"))
        required={"normalized_claim","type","source_type","last_verified_at","verification_status","quality_flags","source_ref","derived_from","review_state"}
        issues=[]; metrics={"claims": c.execute("SELECT count(*) n FROM claims").fetchone()["n"], "schema_missing": sorted(required-cols)}
        bad=[]; topics=[]; blobs=[]; secrets=[]; schema_anomalies=[]
        # Metadata corruption is often old and low-salience, so scan the full bounded table
        # instead of only the freshest rows; output remains capped by `limit`.
        for r in c.execute("SELECT id,status,topic,claim FROM claims ORDER BY updated_at DESC LIMIT 20000").fetchall():
            status = str(r["status"] or "")
            if status not in VALID_CLAIM_STATUSES and len(schema_anomalies) < limit:
                schema_anomalies.append({"id":r["id"],"field":"status","value":short(status,80),"suggested":"uncertain","claim":short(r["claim"],180)})
            topic_reason = topic_integrity_reason(r["topic"])
            if topic_reason and len(schema_anomalies) < limit:
                suggested = "config" if str(r["claim"] or "").lower().startswith("hermes env/config metadata") else self._infer_topic(r["claim"])
                schema_anomalies.append({"id":r["id"],"field":"topic","value":short(r["topic"],80),"suggested":suggested,"reason":topic_reason,"claim":short(r["claim"],180)})
        for r in c.execute("SELECT * FROM claims WHERE status='active' ORDER BY updated_at DESC LIMIT ?", (max(limit*10, limit),)).fetchall():
            q = claim_quality(r["claim"], r["topic"]); bad_frag, why = is_bad_claim_fragment(r["claim"])
            if r["id"].startswith("c_summary_"):
                bad_frag = False; why = ""
            if (q < 0.28 or bad_frag) and len(bad) < limit:
                bad.append({"id":r["id"],"topic":r["topic"],"quality":q,"reason":why or "low quality","claim":short(r["claim"],240)})
            if ((r["topic"] in BAD_TOPICS or str(r["topic"]).isdigit()) and r["topic"] not in CANONICAL_TOPICS) and len(topics) < limit:
                topics.append({"id":r["id"],"topic":r["topic"],"suggested_topic":self._infer_topic(r["claim"]),"claim":short(r["claim"],220)})
            claim_s = str(r["claim"])
            if not r["id"].startswith("c_summary_") and str(r["type"] if "type" in r.keys() else "") != "task_result" and (claim_s.startswith("{") or "\n" in claim_s) and len(blobs) < limit:
                blobs.append({"id":r["id"],"topic":r["topic"],"claim":short(r["claim"],240)})
            if not r["id"].startswith("c_summary_") and str(r["type"] if "type" in r.keys() else "") != "task_result" and is_ephemeral_fragment(r["claim"]) and len(blobs) < limit:
                blobs.append({"id":r["id"],"topic":r["topic"],"claim":short(r["claim"],240),"reason":"system/tool artifact"})
            if (redact_secrets(r["claim"]) != r["claim"] or redact_secrets(str(r["evidence"] or "")) != str(r["evidence"] or "")) and len(secrets) < limit:
                secrets.append({"id":r["id"],"topic":r["topic"],"claim":short(redact_secrets(r["claim"]),240)})
        metrics.update({"low_quality":len(bad),"bad_topics":len(topics),"raw_blobs":len(blobs),"secret_hits":len(secrets),"schema_anomalies":len(schema_anomalies)})
        if metrics["schema_missing"]: issues.append("schema migration incomplete")
        if schema_anomalies: issues.append("claim metadata anomalies need integrity repair")
        if bad: issues.append("low-quality/fragment claims need curation")
        if topics: issues.append("bad topics need retopic")
        if blobs: issues.append("raw logs/json blobs should be summarized or retired")
        if secrets: issues.append("potential secrets require scrub")
        top_artifacts=0
        for r in c.execute("SELECT * FROM claims WHERE status='active' ORDER BY salience DESC, confidence DESC LIMIT 80").fetchall():
            if not r['id'].startswith('c_summary_') and (is_ephemeral_fragment(r['claim']) or str(r['trust_class'] if 'trust_class' in r.keys() else '') in ('tool_log','raw_blob','secret')):
                top_artifacts += 1
        eval_score = 1.0
        try:
            eval_score = float(self._evaluate_retrieval(5, 2500).get('score', 1.0))
        except Exception:
            eval_score = 0.75
        components={
            'db_health': 0.0 if metrics['schema_missing'] else (1.0 - min(1.0, len(schema_anomalies)*0.10)),
            'secret_health': 1.0 - min(1.0, len(secrets)*0.10),
            'semantic_quality': 1.0 - min(1.0, len(bad)*0.03 + len(topics)*0.02 + len(blobs)*0.04),
            'artifact_pollution': 1.0 - min(1.0, top_artifacts*0.06),
            'retrieval_quality': eval_score,
            'contradiction_health': 1.0 - min(1.0, c.execute("SELECT count(*) n FROM contradictions WHERE status='open'").fetchone()['n']*0.015),
        }
        metrics.update({'top_artifacts':top_artifacts,'health_components':components})
        score = sum(components.values()) / max(1, len(components))
        return {"version":"1.2.1-memory-os","health_score":round(score,3),"metrics":metrics,"issues":issues,"schema_anomalies":schema_anomalies,"low_quality":bad,"bad_topics":topics,"raw_blobs":blobs,"secret_hits":secrets}

    def _explain_recall(self, query: str, limit: int = 10, topic: Optional[str]=None) -> List[Dict[str, Any]]:
        rows = self._search(query, limit, True, topic); qt=tokens(query); out=[]
        for r in rows:
            blob = claim_search_text(r.get("claim",""), r.get("normalized_claim",""), r.get("topic",""), r.get("evidence","")); overlap=sorted(qt & tokens(blob))
            parts = r.get("score_parts", {}) or {}
            top_parts = sorted(parts.items(), key=lambda kv: abs(float(kv[1])), reverse=True)[:6]
            reason = ", ".join(f"{k}={float(v):.2f}" for k,v in top_parts)
            out.append({"id":r["id"],"topic":r["topic"],"score":round(float(r.get("score",0)),3),"score_parts":parts,"quality":round(float(r.get("quality",0)),3),"trust_score":round(float(r.get("trust_score",0.55)),3),"confidence":r["confidence"],"salience":r["salience"],"fresh":not self._is_stale(r["freshness_at"]),"risk":r.get("risk","low"),"overlap":overlap[:12],"reason":reason or f"overlap={len(overlap)}","claim":r["claim"]})
        return out


    # ----- v0.9 ideal-memory extensions ---------------------------------
    def _add_secret(self, a: Dict[str, Any]) -> Dict[str, Any]:
        subject=normalize_claim(a.get("subject") or ""); scope=normalize_claim(a.get("scope") or "")
        if not subject or not scope: raise ValueError("subject and scope required")
        typ=slug(a.get("secret_type") or "credential") or "credential"; locator=normalize_claim(a.get("locator") or "")
        value=str(a.get("value") or "").strip(); purpose=normalize_claim(a.get("purpose") or "")
        source=str(a.get("source") or "tool"); ts=now(); h=sha("\0".join([subject.lower(),scope.lower(),typ,locator.lower(),value]))
        sid="sec_"+h[:12]
        with self._connect() as c:
            c.execute("""INSERT INTO secret_index(id,subject,scope,secret_type,locator,value,purpose,source,confidence,salience,status,last_verified_at,created_at,updated_at,hash)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(hash) DO UPDATE SET purpose=excluded.purpose, source=excluded.source, confidence=excluded.confidence, salience=excluded.salience, status='active', updated_at=excluded.updated_at""",
                      (sid,subject,scope,typ,locator,value,purpose,source,clamp(float(a.get("confidence",.85))),clamp(float(a.get("salience",.85))),'active',ts,ts,ts,h))
        claim=f"Secret index: {subject} / {scope} ({typ}) locator={locator or 'n/a'} purpose={purpose or 'n/a'} value=<stored in secret_index>"
        cid=self._add_claim(claim, "secrets", "Structured secret_index entry created; value intentionally redacted in claim.", source, .88, .86)
        self._add_change('secret_upsert', sid, f"{subject}/{scope}/{typ}"); self._render_active_dashboard()
        return {"id":sid,"claim_id":cid,"redacted": True}

    def _query_secrets(self, query: str, limit: int = 10, reveal: bool = False) -> List[Dict[str, Any]]:
        q=(query or "").lower(); limit=max(1,min(int(limit or 10),50)); rows=[]
        for r in self._connect().execute("SELECT * FROM secret_index WHERE status='active' ORDER BY salience DESC, updated_at DESC LIMIT 300").fetchall():
            hay=" ".join(str(r[k] or "") for k in ("subject","scope","secret_type","locator","purpose","source")).lower()
            if not q or any(t in hay for t in tokens(q)) or q in hay:
                d=self._sanitize_row(r)
                if reveal:
                    d["value"] = r["value"]
                    self._audit("secret_reveal", "ok", f"{r['id']} query={short(query,120)}")
                else:
                    d["value"] = "<redacted: set reveal=true only when explicitly needed>" if r["value"] else ""
                d["has_value"] = bool(r["value"]); rows.append(d)
            if len(rows) >= limit: break
        return rows

    def _secret_context(self, query: str, limit: int = 3) -> str:
        rows=self._query_secrets(query, limit, reveal=False)
        if not rows: return ""
        lines=["Secret vault index matches (values redacted; reveal only for explicit secret-requiring tasks):"]
        for r in rows: lines.append(f"- `{r['id']}` {r['subject']} / {r['scope']} type={r['secret_type']} locator={r['locator'] or 'n/a'} purpose={short(r['purpose'],120)}")
        return "\n".join(lines)

    def _recall_plan(self, query: str, limit: int = 8) -> Dict[str, Any]:
        q=(query or "").lower(); topics=[]; types=[]
        mapping=[
            (('memory-wiki','memory_wiki','memory wiki','wiki','claim','claims','recall','pack_context','качест','памят'), 'memory-wiki'),
            (('секрет','secret','token','password','пароль','credential','доступ','secret_index'), 'secrets'),
            (('android','termux','phone','proot','андроид'), 'android'),
            (('hermes','plugin','плагин','tool'), 'hermes'),
            (('сервер','server','ssh','vps','service','systemd'), 'server'),
            (('рустем','rustem','openclaw'), 'openclaw'),
            (('telegram','телеграм','bot','бот'), 'telegram'),
            (('preference','preferences','предпочитает','пользователь'), 'preferences'),
        ]
        for keys,t in mapping:
            if any(k in q for k in keys) and t not in topics: topics.append(t)
        if not topics:
            for r in self._search(query, min(limit,5), False):
                if r['topic'] not in topics: topics.append(r['topic'])
        if any(k in q for k in ('как','инструкция','procedure','установ','deploy','restore','восстанов','патч','patch')): types.append('procedure')
        if any(k in q for k in ('secret','секрет','token','password','пароль','доступ','ssh','credential')): types.append('credential')
        if any(k in q for k in ('ошибка','bug','слом','fix','почини')): types.append('bug')
        if any(k in q for k in ('предпочитает','preference','preferences')): types.append('preference')
        if not types: types=['fact','procedure','environment']
        return {"query":query,"topics":topics[:limit],"types":types[:limit],"secrets_recommended":'secrets' in topics or 'credential' in types,"actions":["query relevant topics", "check contradictions", "use secret reveal only if necessary", "record post_task after config/server changes"]}

    def _post_task(self, a: Dict[str, Any]) -> Dict[str, Any]:
        summary=normalize_claim(a.get("summary") or "");
        if not summary: raise ValueError("summary required")
        topic=self._topic_alias(a.get("topic") or "operations", summary); changed=list(a.get("changed_files") or []); backups=list(a.get("backups") or [])
        verification=normalize_claim(a.get("verification") or ""); services=list(a.get("services") or []); source=a.get("source") or "post_task"; ts=now(); pid="pt_"+sha(summary+str(ts))[:12]
        with self._connect() as c:
            c.execute("INSERT OR IGNORE INTO post_task_log(id,summary,topic,changed_files,backups,verification,services,source,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (pid,summary,topic,json.dumps(changed,ensure_ascii=False),json.dumps(backups,ensure_ascii=False),verification,json.dumps(services,ensure_ascii=False),source,ts))
        parts=[summary]
        if changed: parts.append("changed_files="+", ".join(changed))
        if backups: parts.append("backups="+", ".join(backups))
        if verification: parts.append("verification="+verification)
        if services: parts.append("services="+", ".join(services))
        cid=self._add_claim("; ".join(parts), topic, "Recorded by memory_wiki_post_task", source, .9, .82)
        self._add_change('post_task', cid, summary); self._render_active_dashboard()
        return {"id":pid,"claim_id":cid,"page":str(self._topic_page(topic))}

    def _render_active_dashboard(self) -> str:
        c=self._connect(); lines=["# Active Memory Dashboard", "", f"Updated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now()))}", ""]
        lines += ["## Critical secret index", ""]
        for r in c.execute("SELECT id,subject,scope,secret_type,locator,purpose,updated_at FROM secret_index WHERE status='active' ORDER BY salience DESC, updated_at DESC LIMIT 20").fetchall():
            lines.append(f"- `{r['id']}` **{r['subject']}** / {r['scope']} `{r['secret_type']}` locator={r['locator'] or 'n/a'} — {short(r['purpose'],160)}")
        lines += ["", "## Recent operations", ""]
        for r in c.execute("SELECT * FROM post_task_log ORDER BY created_at DESC LIMIT 20").fetchall():
            lines.append(f"- `{r['id']}` topic={r['topic']}: {short(r['summary'],220)}")
        lines += ["", "## High-salience active claims", ""]
        for r in c.execute("SELECT id,topic,type,claim,confidence,salience FROM claims WHERE status='active' ORDER BY salience DESC, updated_at DESC LIMIT 30").fetchall():
            lines.append(f"- `{r['id']}` topic={r['topic']} type={r['type']} conf={r['confidence']:.2f} sal={r['salience']:.2f}: {short(r['claim'],220)}")
        path=self.dashboard_dir/"active.md"; path.write_text("\n".join(lines)+"\n", encoding="utf-8")
        return str(path)

    def _active_dashboard(self, limit: int = 80) -> Dict[str, Any]:
        path=self._render_active_dashboard(); text=Path(path).read_text(encoding="utf-8")
        return {"path":path,"content":"\n".join(text.splitlines()[:max(10,min(limit,200))])}


    # ----- v1.0 operational memory OS extensions ------------------------
    def _doctor(self, repair: bool=False) -> Dict[str, Any]:
        checks=[]; repairs=[]
        def add(name, ok, detail="", suggested_action=""):
            checks.append({"name":name,"ok":bool(ok),"detail":detail,"suggested_action":suggested_action})
        needed=['claims','evidence','secret_index','post_task_log','backups','decisions','mistakes','project_profiles','task_capsules','entities','relations','audit_log']
        c=self._connect(); existing={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for t in needed: add('table:'+t, t in existing, 'exists' if t in existing else 'missing', 'memory_wiki_repair target=integrity dry_run=false')
        add('db_exists', self.db_path.exists(), str(self.db_path))
        add('degraded_mode', not self._degraded, self._last_io_error or 'normal', 'inspect recovery/ and restore latest good backup if degraded')
        for name,path in [('pages_dir',self.pages_dir),('dashboards_dir',self.dashboard_dir),('backups_dir',self.backups_dir),('snapshots_dir',self.snapshots_dir),('spool_dir',self.spool_dir),('recovery_dir',self.recovery_dir)]: add(name, path.exists(), str(path), 'memory_wiki_repair target=dashboards dry_run=false')
        try:
            qc=c.execute('PRAGMA quick_check').fetchone()[0]; add('sqlite_quick_check', qc=='ok', str(qc), 'restore latest good backup if not ok')
        except Exception as e: add('sqlite_quick_check', False, str(e))
        try:
            with c:
                key='doctor_write_probe'; val=str(now())
                c.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (key, val))
                got=c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0]
                c.execute('DELETE FROM meta WHERE key=?', (key,))
            add('sqlite_write_probe', got==val, 'ok' if got==val else 'readback mismatch', 'restore latest good backup or check filesystem')
        except Exception as e: add('sqlite_write_probe', False, str(e), 'check filesystem space/permissions; inspect spool/recovery')
        try:
            add('wal_checkpoint', True, self._checkpoint_wal('PASSIVE'))
        except Exception as e: add('wal_checkpoint', False, str(e), 'memory_wiki_repair target=integrity dry_run=false')
        try:
            add('wal_checkpoint_full', True, self._checkpoint_wal('FULL'))
        except Exception as e: add('wal_checkpoint_full', False, str(e), 'memory_wiki_repair target=integrity dry_run=false')
        try:
            n=c.execute('SELECT count(*) n FROM claims').fetchone()['n']; add('claims_count', True, str(n))
        except Exception as e: add('claims_count', False, str(e), 'memory_wiki_repair target=integrity dry_run=false')
        try:
            invalid_status=c.execute("SELECT count(*) n FROM claims WHERE status NOT IN ('active','retired','superseded','uncertain')").fetchone()['n']
            bad_topic_count=0
            for r in c.execute("SELECT topic FROM claims LIMIT 10000").fetchall():
                if topic_integrity_reason(r['topic']): bad_topic_count += 1
            add('claim_status_values', invalid_status==0, f'invalid={invalid_status}', 'memory_wiki_repair target=integrity dry_run=false')
            add('claim_topic_values', bad_topic_count==0, f'anomalies={bad_topic_count}', 'memory_wiki_repair target=integrity dry_run=false')
        except Exception as e: add('claim_metadata_values', False, str(e), 'memory_wiki_repair target=integrity dry_run=false')
        try:
            fts_exists='claims_fts' in existing
            if fts_exists:
                cn=c.execute('SELECT count(*) n FROM claims').fetchone()['n']; fn=c.execute('SELECT count(*) n FROM claims_fts').fetchone()['n']
                add('fts_claim_count_match', cn==fn, f'claims={cn} fts={fn}', 'memory_wiki_repair target=fts dry_run=false')
            else: add('fts_exists', False, 'claims_fts missing', 'memory_wiki_repair target=fts dry_run=false')
        except Exception as e: add('fts_claim_count_match', False, str(e), 'memory_wiki_repair target=fts dry_run=false')
        try:
            for d in (self.pages_dir,self.dashboard_dir,self.snapshots_dir,self.spool_dir,self.recovery_dir):
                d.mkdir(parents=True, exist_ok=True); probe=d/'.write_probe'; atomic_write(probe,'ok\n'); probe.unlink(missing_ok=True)
            add('filesystem_writable', True, str(self.root))
        except Exception as e: add('filesystem_writable', False, str(e))
        if repair:
            r=self._repair('all', dry_run=False); repairs=r.get('actions',[])
            if repairs:
                try:
                    c=self._connect(); existing={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                    for item in checks:
                        if item['name'].startswith('table:'):
                            t=item['name'].split(':',1)[1]; item['ok']=t in existing; item['detail']='exists' if item['ok'] else item['detail']
                    try:
                        qc=c.execute('PRAGMA quick_check').fetchone()[0]
                        for item in checks:
                            if item['name']=='sqlite_quick_check': item['ok']=qc=='ok'; item['detail']=str(qc)
                    except Exception: pass
                except Exception: pass
        return {"ok": all(c['ok'] for c in checks), "checks": checks, "repaired": bool(repair), "repairs": repairs, "root": str(self.root)}

    def _backup(self, reason: str='manual') -> Dict[str, Any]:
        self.backups_dir.mkdir(parents=True, exist_ok=True); ts=time.strftime('%Y%m%d_%H%M%S', time.localtime(now()))
        bid='bak_'+ts+'_'+sha(reason)[:8]; path=self.backups_dir/(bid+'.zip')
        tmp_path=path.with_suffix(path.suffix+'.tmp')
        if self._conn:
            try: self._checkpoint_wal('FULL')
            except Exception:
                try: self._checkpoint_wal('PASSIVE')
                except Exception: pass
        written=[]
        try:
            with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for rel in ['memory_wiki.sqlite3']:
                    f=safe_join(self.root, rel)
                    if f.exists() and f.is_file(): z.write(f, rel); written.append(rel)
                for dname in ['pages','dashboards','snapshots']:
                    d=safe_join(self.root, dname)
                    if d.exists():
                        for f in d.rglob('*'):
                            if not f.is_file() or f.is_symlink(): continue
                            arc=str(f.resolve().relative_to(self.root.resolve()))
                            if zip_member_safe(arc): z.write(f, arc); written.append(arc)
                z.writestr('backup_meta.json', json.dumps({'id':bid,'reason':reason,'created_at':now(),'version':'1.2.1','files':len(written)}, ensure_ascii=False, indent=2))
            os.replace(tmp_path, path)
        finally:
            try:
                if tmp_path.exists(): tmp_path.unlink()
            except Exception: pass
        size=path.stat().st_size
        with self._connect() as c: c.execute('INSERT OR REPLACE INTO backups(id,path,reason,size,created_at) VALUES(?,?,?,?,?)',(bid,str(path),reason,size,now()))
        self._add_change('backup', bid, reason)
        return {'id':bid,'path':str(path),'size':size,'reason':reason,'files':len(written)}

    def _list_backups(self, limit:int=20)->List[Dict[str,Any]]:
        rows=[]
        try: rows=[dict(r) for r in self._connect().execute('SELECT * FROM backups ORDER BY created_at DESC LIMIT ?', (max(1,min(limit,100)),)).fetchall()]
        except Exception: pass
        seen={r.get('path') for r in rows}
        for f in sorted(self.backups_dir.glob('*.zip'), key=lambda p:p.stat().st_mtime, reverse=True):
            if str(f) not in seen and len(rows)<limit: rows.append({'id':f.stem,'path':str(f),'reason':'filesystem','size':f.stat().st_size,'created_at':int(f.stat().st_mtime)})
        return rows

    def _restore(self, backup: str) -> Dict[str, Any]:
        b=backup.strip(); rows=self._list_backups(200); match=next((r for r in rows if r['id']==b or r['path']==b), None)
        path=Path(match['path'] if match else b).expanduser()
        if not path.exists(): raise FileNotFoundError(f'backup not found: {backup}')
        if not zipfile.is_zipfile(path): raise ValueError(f'not a zip backup: {path}')
        safety=self._backup('pre-restore safety backup')
        extracted=[]; staged=[]
        with tempfile.TemporaryDirectory(prefix='memory_wiki_restore_', dir=str(self.root)) as td:
            stage=Path(td)
            with zipfile.ZipFile(path) as z:
                infos=[i for i in z.infolist() if not i.is_dir()]
                for info in infos:
                    if not zip_member_safe(info.filename) or not zip_member_regular(info):
                        self._audit('restore','blocked',f'unsafe zip member {info.filename} in {path}')
                        raise ValueError(f'unsafe zip member: {info.filename}')
                    dest=safe_join(stage, info.filename); dest.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as src, open(dest, 'wb') as out:
                        shutil.copyfileobj(src, out)
                    staged.append((info.filename, dest))
            # Lightweight validation before replacing live files.
            staged_db=safe_join(stage, 'memory_wiki.sqlite3')
            if staged_db.exists():
                tc=sqlite3.connect(str(staged_db))
                try:
                    qc=tc.execute('PRAGMA quick_check').fetchone()[0]
                    if qc!='ok': raise ValueError(f'restored sqlite quick_check failed: {qc}')
                finally:
                    tc.close()
            for name, src_path in staged:
                dest=safe_join(self.root, name); dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src_path, dest)
                extracted.append(name)
        if self._conn:
            try: self._conn.close()
            except Exception: pass
        self._conn=None; self._connect(); self._migrate(); self._rebuild_fts(); self._render_all(); self._render_active_dashboard()
        self._add_change('restore', str(path), 'restored from backup'); self._audit('restore','ok',f'{path} files={len(extracted)}')
        return {'restored_from':str(path),'safety_backup':safety,'files':len(extracted)}

    def _add_decision(self, a:Dict[str,Any])->Dict[str,Any]:
        decision=normalize_claim(a.get('decision') or ''); rationale=normalize_claim(a.get('rationale') or ''); topic=self._topic_alias(a.get('topic') or 'decisions', decision); alts=list(a.get('alternatives') or []); ts=now(); h=sha(decision.lower()+rationale.lower()); did='dec_'+h[:12]
        with self._connect() as c: c.execute('INSERT OR IGNORE INTO decisions(id,decision,rationale,topic,alternatives,source,created_at,hash) VALUES(?,?,?,?,?,?,?,?)',(did,decision,rationale,topic,json.dumps(alts,ensure_ascii=False),a.get('source') or 'tool',ts,h))
        cid=self._add_claim('Decision: '+decision+(('; rationale='+rationale) if rationale else ''), topic, 'Alternatives: '+', '.join(alts), a.get('source') or 'tool', .9, .84)
        return {'id':did,'claim_id':cid}

    def _add_mistake(self,a):
        trig=normalize_claim(a.get('trigger') or ''); mis=normalize_claim(a.get('mistake') or ''); fix=normalize_claim(a.get('fix') or ''); prev=normalize_claim(a.get('prevention') or ''); topic=self._topic_alias(a.get('topic') or 'lessons', trig+' '+mis); ts=now(); h=sha(trig.lower()+mis.lower()); mid='mis_'+h[:12]
        with self._connect() as c: c.execute('INSERT OR IGNORE INTO mistakes(id,trigger,mistake,fix,prevention,topic,created_at,hash) VALUES(?,?,?,?,?,?,?,?)',(mid,trig,mis,fix,prev,topic,ts,h))
        cid=self._add_claim(f'Mistake lesson: when {trig}, avoid {mis}; fix={fix or "n/a"}; prevention={prev or "n/a"}', topic, 'Anti-regression memory', 'mistake', .88, .88)
        return {'id':mid,'claim_id':cid}

    def _add_project_profile(self,a):
        pid=slug(a.get('project_id') or 'project'); ts=now()
        raw_blob=json.dumps(a, ensure_ascii=False, default=str)
        raw_secret=bool(secret_scan(raw_blob).get('raw_secret'))
        root=normalize_claim(redact_secrets(a.get('root') or '')); purpose=normalize_claim(redact_secrets(a.get('purpose') or ''))
        commands=[short(redact_secrets(str(x)), 500) for x in list(a.get('commands') or [])[:80]]
        services=[short(redact_secrets(str(x)), 300) for x in list(a.get('services') or [])[:80]]
        notes=normalize_claim(redact_secrets(a.get('notes') or ''))
        stack=a.get('stack') or a.get('stack_json') or {}
        if isinstance(stack, str):
            try: stack=json.loads(stack)
            except Exception: stack={"raw": short(redact_secrets(stack), 800)}
        status=normalize_claim(redact_secrets(a.get('current_status') or ''))
        last_verified=int(a.get('last_verified_at') or (ts if a.get('verified') else 0))
        before=self._table_row('project_profiles', pid, 'project_id')
        if raw_secret:
            self._quarantine_secret('project_profiles', pid, 'payload', raw_blob, 'add_project_profile_raw_secret')
            self._make_secret_index_from_raw('project_profiles', pid, 'payload', raw_blob, json.dumps({'root':root,'purpose':purpose,'commands':commands,'services':services,'notes':notes,'stack':stack,'status':status}, ensure_ascii=False))
        with self._connect() as c:
            c.execute("""INSERT INTO project_profiles(project_id,root,purpose,commands,services,notes,updated_at,stack_json,current_status,last_verified_at,scope,source)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(project_id) DO UPDATE SET root=excluded.root,purpose=excluded.purpose,commands=excluded.commands,services=excluded.services,notes=excluded.notes,updated_at=excluded.updated_at,stack_json=excluded.stack_json,current_status=excluded.current_status,last_verified_at=excluded.last_verified_at,scope=excluded.scope,source=excluded.source""",(pid,root,purpose,json.dumps(commands,ensure_ascii=False),json.dumps(services,ensure_ascii=False),notes,ts,json.dumps(stack,ensure_ascii=False,sort_keys=True),status,last_verified,'project','project_profile'))
        after=self._table_row('project_profiles', pid, 'project_id')
        self._record_mutation('upsert_project_profile','project_profiles',pid,before,after,'memory_wiki_add_project_profile')
        cid=self._add_claim(f'Project profile {pid}: root={root or "n/a"}; purpose={purpose or "n/a"}; commands={commands[:8]}; services={services[:8]}; status={status or "n/a"}; notes={notes}', 'projects', 'Project profile', 'project_profile', .9, .86)
        return {'project_id':pid,'claim_id':cid,'secret_quarantined':raw_secret}


    def _add_task_capsule(self,a):
        def clean_text(v, limit=1200):
            return short(redact_secrets(normalize_claim(str(v or ''))), limit)
        def clean_list(k, item_limit=700):
            out=[]
            for item in list(a.get(k) or []):
                s=clean_text(item, item_limit)
                if s: out.append(s)
            return out[:40]
        raw_blob=json.dumps(a, ensure_ascii=False, default=str)
        raw_secret=bool(secret_scan(raw_blob).get('raw_secret'))
        if raw_secret:
            self._quarantine_secret('task_capsules', 'pending', 'payload', raw_blob, 'add_task_capsule_raw_secret')
        intent=clean_text(a.get('intent') or '', 1600)
        if not intent: raise ValueError('empty task capsule intent')
        topic=self._topic_alias(a.get('topic') or 'tasks', intent); ts=now(); h=sha(intent.lower()+str(ts)); tid='task_'+h[:12]
        fields={k:clean_list(k) for k in ['files','commands','errors','fixes','followups']}
        plan=clean_text(a.get('plan') or '', 2000); verification=clean_text(a.get('verification') or '', 1600)
        with self._connect() as c: c.execute('INSERT OR IGNORE INTO task_capsules(id,intent,topic,plan,files,commands,errors,fixes,verification,followups,created_at,hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(tid,intent,topic,plan,json.dumps(fields['files'],ensure_ascii=False),json.dumps(fields['commands'],ensure_ascii=False),json.dumps(fields['errors'],ensure_ascii=False),json.dumps(fields['fixes'],ensure_ascii=False),verification,json.dumps(fields['followups'],ensure_ascii=False),ts,h))
        evidence='Rich task capsule'
        if raw_secret:
            sid=self._make_secret_index_from_raw('task_capsules', tid, 'payload', raw_blob, json.dumps({'intent':intent,'plan':plan,'fields':fields,'verification':verification}, ensure_ascii=False))
            if sid: evidence += ' [REDACTED_SECRET]'
        summary_parts=[f"Task outcome: {intent}"]
        if plan: summary_parts.append("plan=" + short(plan, 260))
        if verification: summary_parts.append("verification=" + short(verification, 220))
        if fields['files']: summary_parts.append("files=" + ", ".join(fields['files'][:6]))
        if fields['fixes']: summary_parts.append("fixes=" + "; ".join(fields['fixes'][:3]))
        if fields['errors']: summary_parts.append("errors=" + "; ".join(fields['errors'][:3]))
        if fields['followups']: summary_parts.append("followups=" + "; ".join(fields['followups'][:3]))
        claim_text=short("; ".join(summary_parts), 900)
        cid=self._add_claim(claim_text, topic, evidence, 'task_capsule', .9, .84)
        try:
            with self._connect() as c:
                c.execute("UPDATE claims SET type='task_result', derived_from=?, source_ref=?, quality_flags=?, review_state='accepted' WHERE id=?", (tid, f"task_capsules:{tid}", json.dumps(['task_capsule_summary'], ensure_ascii=False), cid))
        except Exception:
            pass
        return {'id':tid,'claim_id':cid}

    def _add_entity(self,a):
        raw_name=str(a.get('name') or '')
        raw_aliases=list(a.get('aliases') or [])
        raw_notes=str(a.get('notes') or '')
        name=normalize_claim(redact_secrets(raw_name))
        et=slug(a.get('entity_type') or 'thing')
        aliases=[normalize_claim(redact_secrets(str(x))) for x in raw_aliases if normalize_claim(redact_secrets(str(x)))]
        notes=normalize_claim(redact_secrets(raw_notes))
        h=sha(name.lower()+et); eid='ent_'+h[:12]
        if not name: raise ValueError('empty entity name')
        before=self._table_row('entities', eid)
        if secret_scan(raw_name+' '+json.dumps(raw_aliases, ensure_ascii=False)+' '+raw_notes).get('raw_secret'):
            raw_payload=raw_name+'\n'+json.dumps(raw_aliases, ensure_ascii=False)+'\n'+raw_notes
            self._quarantine_secret('entities', eid, 'payload', raw_payload, 'add_entity_raw_secret')
            self._make_secret_index_from_raw('entities', eid, 'payload', raw_payload, name+'\n'+json.dumps(aliases, ensure_ascii=False)+'\n'+notes)
        with self._connect() as c: c.execute('INSERT OR REPLACE INTO entities(id,name,entity_type,aliases,notes,updated_at,hash) VALUES(?,?,?,?,?,?,?)',(eid,name,et,json.dumps(aliases,ensure_ascii=False),notes,now(),h))
        self._record_mutation('upsert_entity','entities',eid,before,self._table_row('entities', eid),'memory_wiki_add_entity')
        return {'id':eid}

    def _add_relation(self,a):
        raw_subj=str(a.get('subject') or ''); raw_obj=str(a.get('object') or ''); raw_ev=str(a.get('evidence') or '')
        subj=normalize_claim(redact_secrets(raw_subj)); pred=slug(a.get('predicate') or 'related_to'); obj=normalize_claim(redact_secrets(raw_obj)); evidence=normalize_claim(redact_secrets(raw_ev))
        h=sha(subj.lower()+pred+obj.lower()); rid='rel_'+h[:12]
        if not subj or not obj: raise ValueError('empty relation endpoint')
        before=self._table_row('relations', rid)
        if secret_scan(raw_subj+' '+raw_obj+' '+raw_ev).get('raw_secret'):
            raw_payload=raw_subj+'\n'+raw_obj+'\n'+raw_ev
            self._quarantine_secret('relations', rid, 'payload', raw_payload, 'add_relation_raw_secret')
            self._make_secret_index_from_raw('relations', rid, 'payload', raw_payload, subj+'\n'+obj+'\n'+evidence)
        with self._connect() as c: c.execute('INSERT OR IGNORE INTO relations(id,subject,predicate,object,confidence,evidence,created_at,hash) VALUES(?,?,?,?,?,?,?,?)',(rid,subj,pred,obj,clamp(float(a.get('confidence',.8))),evidence,now(),h))
        self._record_mutation('upsert_relation','relations',rid,before,self._table_row('relations', rid),'memory_wiki_add_relation')
        return {'id':rid}

    def _graph_query(self, query:str, limit:int=20)->Dict[str,Any]:
        q=str(query or '').lower(); qtokens=tokens(q); ents=[]; rels=[]; c=self._connect(); lim=max(1,min(int(limit or 20),200))
        for r in c.execute('SELECT * FROM entities ORDER BY updated_at DESC LIMIT 500').fetchall():
            hay=(r['name']+' '+r['aliases']+' '+r['notes']).lower()
            if (q and q in hay) or any(t in hay for t in qtokens): ents.append(self._sanitize_row(r))
            if len(ents)>=lim: break
        names={e['name'] for e in ents}
        for r in c.execute('SELECT * FROM relations ORDER BY created_at DESC LIMIT 1000').fetchall():
            hay=(r['subject']+' '+r['predicate']+' '+r['object']+' '+r['evidence']).lower()
            if (q and q in hay) or r['subject'] in names or r['object'] in names or any(t in hay for t in qtokens): rels.append(self._sanitize_row(r))
            if len(rels)>=lim: break
        return {'entities':ents[:lim], 'relations':rels[:lim]}

    def _get_project_context(self, project_id: str, query: str = "", limit: int = 20) -> Dict[str,Any]:
        pid=slug(project_id or current_project_id() or 'project'); lim=max(1,min(int(limit or 20),80)); c=self._connect()
        profile=c.execute("SELECT * FROM project_profiles WHERE project_id=?", (pid,)).fetchone()
        q=query or pid
        claims=[self._sanitize_row(r) for r in self._search(q, lim, False) if (r.get('project_id') in ('', pid) or pid in str(r.get('claim','')).lower())]
        tasks=[]
        for r in c.execute("SELECT * FROM task_capsules ORDER BY created_at DESC LIMIT 120").fetchall():
            blob=' '.join(str(r[k]) for k in r.keys()).lower()
            if pid in blob or any(t in blob for t in tokens(q)):
                tasks.append(self._sanitize_row(r))
            if len(tasks)>=lim: break
        graph=self._graph_query(pid + ' ' + q, lim)
        return {"project_id":pid,"profile":self._sanitize_row(profile) if profile else None,"claims":claims[:lim],"task_capsules":tasks[:lim],"graph":graph}

    def _transaction(self, operations: List[Dict[str,Any]], mode: str = "suggest", reason: str = "") -> Dict[str,Any]:
        mode=(mode or 'suggest').lower(); ops=list(operations or [])[:50]; batch_id='batch_'+sha(json.dumps(ops, ensure_ascii=False, sort_keys=True, default=str)+str(now()))[:12]
        backup=None; results=[]
        if mode == 'apply_with_backup':
            backup=self._backup('transaction_'+batch_id)
            mode='apply'
        for op in ops:
            name=str(op.get('tool') or op.get('operation') or '').strip()
            args=dict(op.get('args') or {k:v for k,v in op.items() if k not in ('tool','operation','args')})
            try:
                if mode == 'suggest':
                    if name in ('memory_wiki_compress_topic','memory_wiki_compile_topic','compile_topic'):
                        results.append({'operation':name,'result':self._compile_topic(args.get('topic') or 'general','suggest',int(args.get('limit',50)),args.get('summary_type') or 'summary')})
                    elif name in ('memory_wiki_scrub_secrets','scrub_secrets'):
                        results.append({'operation':name,'result':self._scrub_secrets(False,int(args.get('limit',200)))})
                    elif name in ('memory_wiki_normalize_topics','normalize_topics'):
                        results.append({'operation':name,'result':self._normalize_topics('suggest',int(args.get('limit',100)))})
                    elif name in ('memory_wiki_immune_scan','immune_scan'):
                        results.append({'operation':name,'result':self._immune_scan('suggest',int(args.get('limit',100)))})
                    elif name in ('memory_wiki_repair','repair'):
                        results.append({'operation':name,'result':self._repair(args.get('target') or 'all', True)})
                    elif name in ('memory_wiki_update_claim','update_claim','memory_wiki_rewrite_claim','rewrite_claim'):
                        cid=args.get('claim_id') or ''; results.append({'operation':name,'before':self._table_row('claims', cid),'dry_run':True})
                    else:
                        results.append({'operation':name,'dry_run':True,'note':'suggest mode: operation not executed'})
                else:
                    before_count=self._connect().execute("SELECT count(*) n FROM claims").fetchone()['n']
                    if name in ('memory_wiki_update_claim','update_claim'):
                        res=self._update_claim(args)
                    elif name in ('memory_wiki_rewrite_claim','rewrite_claim'):
                        res=self._rewrite_claim(args)
                    elif name in ('memory_wiki_merge_claims','merge_claims'):
                        res=self._merge_claims(args)
                    elif name in ('memory_wiki_compress_topic','memory_wiki_compile_topic','compile_topic'):
                        res=self._compile_topic(args.get('topic') or 'general','apply',int(args.get('limit',50)),args.get('summary_type') or 'summary')
                    elif name in ('memory_wiki_scrub_secrets','scrub_secrets'):
                        res=self._scrub_secrets(True,int(args.get('limit',200)))
                    elif name in ('memory_wiki_normalize_topics','normalize_topics'):
                        res=self._normalize_topics('apply',int(args.get('limit',100)))
                    elif name in ('memory_wiki_immune_scan','immune_scan'):
                        res=self._immune_scan('apply',int(args.get('limit',100)))
                    elif name in ('memory_wiki_repair','repair'):
                        res=self._repair(args.get('target') or 'all', False)
                    else:
                        raise ValueError(f'unsupported transaction operation: {name}')
                    after_count=self._connect().execute("SELECT count(*) n FROM claims").fetchone()['n']
                    mid=self._record_mutation('transaction_operation','batch',batch_id,{"claims":before_count},{"claims":after_count,"result":res},reason or name,batch_id,False)
                    results.append({'operation':name,'mutation_id':mid,'result':res})
            except Exception as e:
                results.append({'operation':name,'error':str(e)})
        self._audit('transaction', 'ok' if not any('error' in r for r in results) else 'partial', f'{batch_id} ops={len(ops)} mode={mode}')
        return {"batch_id":batch_id,"mode":mode,"backup":backup,"results":results,"errors":[r for r in results if 'error' in r]}

    def _export_bundle(self, a: Dict[str,Any]) -> Dict[str,Any]:
        c=self._connect(); limit=max(1,min(int(a.get('limit',500) or 500),5000)); topic=self._topic_alias(a.get('topic') or '', '') if a.get('topic') else ''; project_id=slug(a.get('project_id') or '') if a.get('project_id') else ''; scope=str(a.get('scope') or '')
        where=["1=1"]; params=[]
        if topic: where.append("topic=?"); params.append(topic)
        if project_id: where.append("(project_id=? OR claim LIKE ?)"); params.extend([project_id, f'%{project_id}%'])
        if scope: where.append("scope=?"); params.append(scope)
        sql="SELECT * FROM claims WHERE "+" AND ".join(where)+" ORDER BY updated_at DESC LIMIT ?"; params.append(limit)
        claims=[self._sanitize_row(r) for r in c.execute(sql, params).fetchall()]
        claim_ids=[r['id'] for r in claims]
        evidence=[]
        if claim_ids:
            qs=','.join('?' for _ in claim_ids[:900])
            evidence=[self._sanitize_row(r) for r in c.execute(f"SELECT * FROM evidence WHERE claim_id IN ({qs}) ORDER BY created_at DESC LIMIT ?", claim_ids[:900]+[limit]).fetchall()]
        payload={
            'format':'memory-wiki-sync-bundle/v1', 'created_at':now(), 'source_home':str(self.home),
            'filters':{'topic':topic,'project_id':project_id,'scope':scope,'limit':limit},
            'claims':claims, 'evidence':evidence,
            'project_profiles':[self._sanitize_row(r) for r in c.execute("SELECT * FROM project_profiles ORDER BY updated_at DESC LIMIT ?", (min(limit,500),)).fetchall()],
            'entities':[self._sanitize_row(r) for r in c.execute("SELECT * FROM entities ORDER BY updated_at DESC LIMIT ?", (min(limit,500),)).fetchall()],
            'relations':[self._sanitize_row(r) for r in c.execute("SELECT * FROM relations ORDER BY created_at DESC LIMIT ?", (min(limit,800),)).fetchall()],
            'secret_index':[self._sanitize_row(r) for r in c.execute("SELECT id,subject,scope,secret_type,locator,'' as value,purpose,source,confidence,salience,status,last_verified_at,created_at,updated_at,hash FROM secret_index ORDER BY updated_at DESC LIMIT ?", (min(limit,500),)).fetchall()],
        }
        payload_hash=sha(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        path=''
        if bool(a.get('write_file', True)):
            outdir=self.root/'sync-bundles'; outdir.mkdir(parents=True, exist_ok=True)
            path=str(outdir/(time.strftime('%Y%m%d_%H%M%S', time.localtime(now()))+f'_{payload_hash[:10]}.json'))
            atomic_write(Path(path), json.dumps(payload, ensure_ascii=False, indent=2)+"\n")
        bid='sync_'+payload_hash[:12]
        with c: c.execute("INSERT OR REPLACE INTO sync_bundles(id,path,summary,payload_hash,direction,created_at) VALUES(?,?,?,?,?,?)", (bid,path,json.dumps(payload['filters'],ensure_ascii=False),payload_hash,'export',now()))
        return {'id':bid,'path':path,'payload_hash':payload_hash,'counts':{k:len(v) for k,v in payload.items() if isinstance(v,list)},'payload':payload if not bool(a.get('write_file', True)) else {}}

    def _import_bundle(self, a: Dict[str,Any]) -> Dict[str,Any]:
        payload=a.get('payload') or {}
        p=a.get('path') or ''
        if not payload and p:
            payload=json.loads(Path(p).read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or not str(payload.get('format','')).startswith('memory-wiki-sync-bundle'):
            raise ValueError('invalid memory-wiki sync bundle')
        mode=(a.get('mode') or 'suggest').lower(); counts={}; created=[]
        if mode == 'suggest':
            return {'mode':mode,'counts':{k:len(v) for k,v in payload.items() if isinstance(v,list)},'filters':payload.get('filters',{}),'would_import':True}
        c=self._connect()
        with c:
            for r in payload.get('claims') or []:
                claim=normalize_claim(r.get('claim') or '')
                if not claim or secret_scan(claim + ' ' + str(r.get('evidence',''))).get('raw_secret'):
                    continue
                cid=self._add_claim(claim, r.get('topic') or self._infer_topic(claim), r.get('evidence') or '', 'memory_wiki_import_bundle', float(r.get('confidence',.65)), float(r.get('salience',.55)))
                created.append(cid); counts['claims']=counts.get('claims',0)+1
            for r in payload.get('project_profiles') or []:
                self._add_project_profile(r); counts['project_profiles']=counts.get('project_profiles',0)+1
            for r in payload.get('entities') or []:
                self._add_entity(r); counts['entities']=counts.get('entities',0)+1
            for r in payload.get('relations') or []:
                self._add_relation(r); counts['relations']=counts.get('relations',0)+1
        payload_hash=sha(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)); bid='sync_'+payload_hash[:12]
        with c: c.execute("INSERT OR REPLACE INTO sync_bundles(id,path,summary,payload_hash,direction,created_at) VALUES(?,?,?,?,?,?)", (bid,p,json.dumps(payload.get('filters',{}),ensure_ascii=False),payload_hash,'import',now()))
        self._rebuild_fts(); self._render_all(); self._audit('import_bundle','ok',f'{bid} counts={counts}')
        return {'mode':mode,'id':bid,'payload_hash':payload_hash,'counts':counts,'created_claims':created[:100]}

    def _apply_user_correction(self,a):
        corr=normalize_claim(a.get('correction') or ''); target=a.get('target_claim_id') or ''; topic=self._topic_alias(a.get('topic') or 'corrections', corr); changed=[]
        with self._connect() as c:
            if target:
                c.execute("UPDATE claims SET status='superseded', updated_at=? WHERE id=?", (now(),target)); changed.append(target)
            else:
                for r in self._search(corr, 5, True):
                    if r['status']=='active' and r['confidence']<.95:
                        c.execute("UPDATE claims SET status='uncertain', updated_at=? WHERE id=?", (now(),r['id'])); changed.append(r['id'])
        cid=self._add_claim('User correction: '+corr, topic, 'Explicit user correction captured by memory_wiki_apply_user_correction', 'explicit_user_correction', .98, .95)
        return {'claim_id':cid,'updated_old_claims':changed}

    def _session_context_candidates(self, query: str, max_items: int = 18) -> List[Dict[str, Any]]:
        """Pull relevant snippets from persisted Hermes sessions for context packing."""
        qtok=tokens(query); items=[]; base=Path(os.environ.get('HERMES_HOME') or str(Path.home()/'.hermes'))/'sessions'
        try:
            files=sorted(base.glob('session_*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:120]
        except Exception:
            return []
        for p in files:
            try:
                data=json.loads(p.read_text(encoding='utf-8', errors='ignore'))
                msgs=data.get('messages') or []
                parts=[]
                for m in msgs:
                    role=str(m.get('role',''))
                    if role not in ('user','assistant'):
                        continue
                    content=m.get('content')
                    if not isinstance(content, str):
                        content=json.dumps(content, ensure_ascii=False)[:1200]
                    if 'CONTEXT COMPACTION' in content or 'System note:' in content:
                        continue
                    content=redact_secrets(content)
                    if secret_scan(content).get('raw_secret'):
                        continue
                    mt=tokens(content); overlap=len(qtok & mt) if qtok else 1
                    if qtok and overlap <= 0:
                        continue
                    parts.append((overlap, role, short(content, 360)))
                if not parts:
                    continue
                parts=sorted(parts, key=lambda x:x[0], reverse=True)[:4]
                summary=' | '.join(f"{role}: {txt}" for _,role,txt in parts)
                score=sum(x[0] for x in parts)/(len(qtok) or 1)
                items.append({'session_id':data.get('session_id') or p.stem.replace('session_',''), 'updated':data.get('last_updated') or time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(p.stat().st_mtime)), 'score':score, 'summary':summary})
            except Exception:
                continue
        return sorted(items, key=lambda x:x['score'], reverse=True)[:max_items]

    def _llm_pack_context(self, query: str, candidate_context: str, max_chars: int) -> str:
        """Use the configured local GPT-5.5-compatible endpoint as a secondary context analyst."""
        if not candidate_context.strip() or os.environ.get('MEMORY_WIKI_LLM_PACK','0').lower() in ('0','false','no','off'):
            return ''
        cfg_path=Path(os.environ.get('HERMES_CONFIG') or str(Path.home()/'.hermes'/'config.yaml'))
        raw=''
        try:
            raw=cfg_path.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            pass
        def grab(key: str, default: str='') -> str:
            m=re.search(rf'(?m)^\s*{re.escape(key)}:\s*([^\n#]+)', raw)
            return (m.group(1).strip().strip('"\'') if m else default)
        base_url=os.environ.get('MEMORY_WIKI_LLM_BASE_URL') or grab('base_url','http://127.0.0.1:18646/v1')
        api_key=os.environ.get('MEMORY_WIKI_LLM_API_KEY') or grab('api_key','noop')
        model=os.environ.get('MEMORY_WIKI_LLM_MODEL') or grab('model','gpt-5.5')
        if not base_url:
            return ''
        endpoint=base_url.rstrip('/') + '/chat/completions'
        budget=max(700, min(max_chars, 30000))
        system=("Ты вторичная модель gpt-5.5 для memory_wiki_pack_context. "
                "Проанализируй кандидаты из claims/task_capsules/session history/graph/secret index и верни только данные, которые надо подгрузить в рабочий чат. "
                "Не раскрывай секреты; сохраняй ids, paths, команды и конкретные выводы. Без рассуждений и воды.")
        user=(f"QUERY:\n{query}\n\nMAX_CHARS: {budget}\n\nCANDIDATE_CONTEXT:\n{candidate_context[:90000]}\n\n"
              "Верни компактный packed context в markdown bullets, отсортированный по полезности.")
        payload={'model':model,'messages':[{'role':'system','content':system},{'role':'user','content':user}], 'max_tokens':max(512, min(8192, budget//2)), 'temperature':0}
        try:
            req=urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers={'Content-Type':'application/json','Authorization':f'Bearer {api_key}'}, method='POST')
            with urllib.request.urlopen(req, timeout=float(os.environ.get('MEMORY_WIKI_LLM_TIMEOUT','45'))) as resp:
                obj=json.loads(resp.read().decode('utf-8','ignore'))
            text=obj.get('choices',[{}])[0].get('message',{}).get('content','')
            text=redact_secrets(text)
            if text and not secret_scan(text).get('raw_secret'):
                return text[:budget]
        except Exception:
            return ''
        return ''

    def _pack_context(self, query:str, max_chars:int=MAX_PREFETCH_CHARS)->Dict[str,Any]:
        max_chars=max(800, min(int(max_chars or MAX_PREFETCH_CHARS), 60000))
        plan=self._recall_plan(query, 12); rows=self._search(query, 60, False); graph=self._graph_query(query, 12)
        secrets=self._query_secrets(query, 8, False) if plan.get('secrets_recommended') else []
        qtok=set(tokens(query)); omitted={'secret_or_quarantined':0,'low_relevance':0,'artifact_or_low_quality':0,'secrets_not_requested':0}; sources={'claims':len(rows),'secrets':len(secrets),'relations':len(graph.get('relations',[])),'task_capsules':0,'sessions':0,'llm_refined':False,'sectioned':True}
        if not plan.get('secrets_recommended'):
            omitted['secrets_not_requested']=1
        sections=[
            ('recall_plan','## Recall plan',1000),
            ('preferences','## User operating preferences / constraints',960),
            ('procedures','## Procedures / runbooks',930),
            ('secrets_policy','## Secret storage policy',920),
            ('environment','## Current environment / project facts',900),
            ('projects','## Project profiles',890),
            ('task_outcomes','## Recent task outcomes',870),
            ('source_policy','## Source ingestion policy',850),
            ('secrets','## Secret index matches (redacted)',840),
            ('contradictions','## Open contradictions / uncertain facts',800),
            ('relations','## Entity graph relations',760),
            ('sessions','## Relevant recent sessions',720),
            ('other','## Other relevant facts',650),
        ]
        buckets={k:[] for k,_,_ in sections}; seen=set(); plan_topics=set(plan.get('topics') or [])
        def add(bucket,label,text,prio):
            text=redact_secrets(str(text or '')).strip()
            if not text:
                return
            if secret_scan(text).get('raw_secret'):
                omitted['secret_or_quarantined']+=1; return
            if is_ephemeral_fragment(text):
                omitted['artifact_or_low_quality']+=1; return
            key=sha(f"{bucket}:{label}:{text}")[:16]
            if key not in seen:
                seen.add(key); buckets.setdefault(bucket,[]).append((prio,label,short(text,700)))
        add('recall_plan','plan',json.dumps(plan,ensure_ascii=False),1000)
        add('source_policy','current_query', json.dumps(source_policy_for('tool'), ensure_ascii=False), 850)
        for s in secrets:
            add('secrets','secret_index', f"`{s['id']}` {s['subject']} / {s['scope']} type={s['secret_type']} locator={s['locator']} purpose={s['purpose']}", 900)
        for r in rows:
            if r.get('status') != 'active' or str(r.get('risk','low')) == 'secret' or int(r.get('quarantined_at') or 0) > 0:
                omitted['secret_or_quarantined']+=1; continue
            typ=str(r.get('type') or 'fact'); topic=str(r.get('topic') or '')
            if plan_topics and topic not in plan_topics and typ not in ('preference','constraint'):
                if not (plan.get('secrets_recommended') and 'секрет' in str(r.get('claim','')).lower()):
                    omitted['low_relevance']+=1; continue
            trust_class=str(r.get('trust_class') or '')
            if is_ephemeral_fragment(r.get('claim','')) or trust_class in ('tool_log','raw_blob','secret') or typ == 'source_artifact' or (float(r.get('quality') or 0) < 0.38 and typ not in ('procedure','preference','task_result')):
                omitted['artifact_or_low_quality']+=1; continue
            freshness = 'fresh' if not self._is_stale(r['freshness_at']) else 'stale'
            line=f"`{r['id']}` topic={topic} type={typ} score={float(r.get('score',0)):.2f} conf={r['confidence']:.2f} sal={r['salience']:.2f} trust={float(r.get('trust_score',.55)):.2f} {freshness}: {r['claim']}"
            bucket='other'
            if topic == 'secrets' and plan.get('secrets_recommended'):
                bucket='secrets_policy'
            elif typ in ('preference','constraint') or topic in ('preferences','user-preferences','workflow_preferences'):
                bucket='preferences'
            elif typ in ('procedure','decision','lesson') or str(r.get('trust_class','')) in ('procedure','decision','lesson'):
                bucket='procedures'
            elif typ in ('environment','fact') and topic not in ('preferences',):
                bucket='environment'
            if topic == 'secrets' and not plan.get('secrets_recommended'):
                omitted['secrets_not_requested']+=1; continue
            add(bucket,'claim',line,int(float(r.get('score',0))*100))
        for rel in graph.get('relations',[]): add('relations','relation', f"{rel['subject']} -[{rel['predicate']}]-> {rel['object']} conf={rel['confidence']}", 700)
        try:
            c=self._connect()
            profile_rows=c.execute("SELECT * FROM project_profiles ORDER BY updated_at DESC LIMIT 40").fetchall()
            for p in profile_rows:
                blob=' '.join(str(p[k]) for k in p.keys()).lower()
                overlap=len(qtok & set(tokens(blob))) if qtok else 1
                if qtok and overlap <= 0 and str(p['project_id']).lower() not in str(query or '').lower():
                    continue
                add('projects','project_profile', f"`{p['project_id']}` root={p['root']}; purpose={p['purpose']}; commands={p['commands']}; services={p['services']}; status={p['current_status'] if 'current_status' in p.keys() else ''}; notes={p['notes']}", 900 + overlap*40)
            for k in c.execute("SELECT * FROM contradictions WHERE status='open' ORDER BY created_at DESC LIMIT 30").fetchall():
                add('contradictions','contradiction', f"`{k['id']}` {k['claim_a']} ↔ {k['claim_b']}: {k['reason']} severity={k['severity'] if 'severity' in k.keys() else 'possible'}", 790)
            task_rows=c.execute("SELECT * FROM task_capsules ORDER BY created_at DESC LIMIT 40").fetchall()
            sources['task_capsules']=len(task_rows)
            for t in task_rows:
                blob=' '.join([str(t['intent']), str(t['topic']), str(t['plan']), str(t['files']), str(t['commands']), str(t['errors']), str(t['fixes']), str(t['verification']), str(t['followups'])])
                overlap=len(qtok & set(tokens(blob))) if qtok else 1
                if qtok and overlap <= 0:
                    omitted['low_relevance']+=1; continue
                add('task_outcomes','task_capsule', f"`{t['id']}` topic={t['topic']} intent={t['intent']}; plan={t['plan']}; files={t['files']}; commands={t['commands']}; errors={t['errors']}; fixes={t['fixes']}; verification={t['verification']}; followups={t['followups']}", 880 + overlap*35)
        except Exception as e:
            add('other','task_capsule_error', str(e), 100)
        session_items=[] if os.environ.get('MEMORY_WIKI_INCLUDE_SESSIONS_IN_PACK','0').lower() not in ('1','true','yes') else self._session_context_candidates(query, max_items=18)
        sources['sessions']=len(session_items)
        for s in session_items:
            add('sessions','session', f"`{s['session_id']}` {s['updated']} score={s['score']:.2f}: {s['summary']}", 820 + int(s['score']*60))
        out=[]; used=0; chunk_count=0
        for key,title,base_prio in sections:
            items=sorted(buckets.get(key,[]), key=lambda x:x[0], reverse=True)
            if not items: continue
            header=title
            if used+len(header)+1>max_chars: break
            out.append(header); used+=len(header)+1
            # Без жёсткого per-section бюджета: режем только общим max_chars, чтобы сильные секции не душились фиксированными квотами.
            for pr,label,text in items:
                line=f"- [{label}] {text}"
                if used+len(line)+1>max_chars: continue
                out.append(line); used+=len(line)+1; chunk_count+=1
        context='\n'.join(out)
        refined=self._llm_pack_context(query, context, max_chars)
        if refined and not is_ephemeral_fragment(refined):
            context=refined; used=len(context); sources['llm_refined']=True
        elif refined:
            omitted['artifact_or_low_quality']+=1
        return {'query':query,'max_chars':max_chars,'used_chars':used,'context':context,'plan':plan,'omitted':omitted,'chunk_count':chunk_count,'sources':sources}


    def _archive_source_artifact(self, claim_id: str, artifact_type: str, text: str, source_ref: str = "") -> str:
        red = short(redact_secrets(text), 2200)
        h = sha(f"claims:{claim_id}:{artifact_type}:{red}")
        aid = "art_" + h[:12]
        with self._connect() as c:
            c.execute("INSERT OR IGNORE INTO source_artifacts(id,source_table,source_id,artifact_type,redacted_excerpt,source_ref,status,created_at,hash) VALUES(?,?,?,?,?,?,?,?,?)", (aid, "claims", claim_id, artifact_type, red, source_ref or f"claims:{claim_id}", "archived", now(), h))
        return aid

    def _evaluate_retrieval(self, limit:int=10, max_chars:int=3800)->Dict[str,Any]:
        limit=max(1,min(int(limit or 10),50)); max_chars=max(800,min(int(max_chars or 3800),12000)); c=self._connect()
        cases=[dict(r) for r in c.execute("SELECT * FROM retrieval_eval_cases ORDER BY updated_at DESC LIMIT 100").fetchall()]
        if not cases:
            return {"cases":0,"score":0.0,"results":[],"summary":{"passed":0,"failed":0}}
        results=[]; passed=0; leak_cases=0
        for case in cases:
            q=case['query']; rows=self._search(q, limit, False); pack=self._pack_context(q, max_chars)
            text=("\n".join([str(r.get('claim','')) for r in rows]) + "\n" + str(pack.get('context','')))
            topics={str(r.get('topic','')) for r in rows}
            must_topics=json.loads(case.get('must_topics') or '[]'); must_not_topics=json.loads(case.get('must_not_topics') or '[]')
            must_include=json.loads(case.get('must_include') or '[]'); must_not_include=json.loads(case.get('must_not_include') or '[]')
            misses=[t for t in must_topics if t and t not in topics and t not in str(pack.get('context',''))]
            bad_topics=[t for t in must_not_topics if t and (t in topics or t in str(pack.get('context','')))]
            missing_text=[x for x in must_include if x and x.lower() not in text.lower()]
            forbidden=[x for x in must_not_include if x and x.lower() in text.lower()]
            artifact_hits=[r.get('id') for r in rows if is_ephemeral_fragment(r.get('claim','')) or str(r.get('trust_class','')) in ('tool_log','raw_blob','secret')]
            secret_leak=bool(secret_scan(text).get('raw_secret'))
            ok=not (misses or bad_topics or missing_text or forbidden or artifact_hits or secret_leak)
            if ok: passed+=1
            if artifact_hits or secret_leak or forbidden: leak_cases+=1
            results.append({"id":case['id'],"query":q,"ok":ok,"topics":sorted(topics),"misses":misses,"bad_topics":bad_topics,"missing_text":missing_text,"forbidden":forbidden,"artifact_hits":artifact_hits,"secret_leak":secret_leak,"rows":len(rows),"pack_chunks":pack.get('chunk_count',0)})
        score=round(passed/max(1,len(cases)),3)
        return {"cases":len(cases),"passed":passed,"failed":len(cases)-passed,"score":score,"leak_cases":leak_cases,"results":results}

    def _migrate_secrets_from_claims(self, apply:bool=True, limit:int=100)->Dict[str,Any]:
        candidates=[]
        pat=re.compile(r'(?i)(password|пароль|token|токен|api[_ -]?key|secret|credential|логин|login|ssh|\.env)')
        for r in self._connect().execute("SELECT * FROM claims WHERE status='active' ORDER BY salience DESC LIMIT ?", (max(1,min(limit,500)),)).fetchall():
            text=(r['claim']+' '+r['evidence'])
            if pat.search(text): candidates.append({'claim_id':r['id'],'topic':r['topic'],'text':short(text,500)})
        created=[]
        if apply:
            for cnd in candidates:
                created.append(self._add_secret({'subject':cnd['topic'], 'scope':'migrated-from-claim', 'secret_type':'credential-note', 'locator':'claim:'+cnd['claim_id'], 'value':'', 'purpose':cnd['text'], 'source':'secret_migration'}))
        return {'candidates':candidates,'created':created,'applied':apply}

    def _scrub_secrets(self, apply: bool=False, limit: int=200) -> Dict[str, Any]:
        """Redact raw secrets already stored in memory tables without surfacing them."""
        c=self._connect(); limit=max(1,min(int(limit or 200),1000)); hits=[]; updated=0; secret_refs=[]
        targets=[('claims','id',['claim','evidence','source']),('evidence','id',['text','source']),('review_queue','id',['candidate','evidence','suggested_claim','reason']),('secret_index','id',['subject','scope','purpose','source']),('task_capsules','id',['intent','plan','files','commands','errors','fixes','verification','followups']),('entities','id',['name','aliases','notes']),('relations','id',['subject','object','evidence']),('project_profiles','project_id',['root','purpose','commands','services','notes','stack_json','current_status']),('post_task_log','id',['summary','changed_files','backups','verification','services'])]
        for table, pk, fields in targets:
            rows=c.execute(f"SELECT * FROM {table} LIMIT 5000").fetchall()
            for r in rows:
                rid=r[pk]; changes={}
                for field in fields:
                    original=str(r[field] or '')
                    scan=secret_scan(original)
                    artifact = scrub_memory_artifacts(original)
                    artifact_changed = artifact != original
                    if not scan.get('raw_secret') and not artifact_changed:
                        continue
                    redacted=scan.get('redacted') or redact_secrets(original)
                    if artifact_changed:
                        redacted=redact_secrets(artifact)
                    hits.append({'table':table,'id':rid,'field':field,'risk':scan.get('risk',0),'findings':scan.get('findings',[])[:3],'sample':short(redacted,240)})
                    if apply:
                        try:
                            if scan.get('raw_secret'):
                                sid=self._make_secret_index_from_raw(table, rid, field, original, redacted)
                                if sid: secret_refs.append(sid)
                                marker=f"[REDACTED_SECRET:{sid or 'unknown'}]"
                                redacted=REDACTION_TOKEN_RE.sub(marker, redacted)
                                self._quarantine_secret(table, rid, field, original, 'memory_wiki_scrub_secrets')
                            changes[field]=short(redacted, 4000 if table!='claims' or field!='evidence' else 2000)
                        except Exception as e:
                            hits[-1]['error']=str(e)
                            continue
                    if len(hits) >= limit:
                        break
                if apply and changes:
                    sets=', '.join(f"{k}=?" for k in changes)
                    params=list(changes.values())+[rid]
                    with c:
                        c.execute(f"UPDATE {table} SET {sets} WHERE {pk}=?", params)
                        if table=='claims':
                            try:
                                status = 'retired' if is_ephemeral_fragment(changes.get('claim', str(r['claim'] or ''))) else str(r['status'] or 'active')
                                c.execute("UPDATE claims SET updated_at=?, risk='low', status=? WHERE id=?", (now(), status, rid))
                            except Exception: pass
                    updated+=1
                    if table=='claims': self._upsert_fts(rid)
                if len(hits) >= limit:
                    break
            if len(hits) >= limit:
                break
        if apply:
            self._rebuild_fts(); self._render_active_dashboard(); self._audit('scrub_secrets','ok',f'hits={len(hits)} updated={updated}')
        return {'applied':apply,'hits':hits,'hit_count':len(hits),'updated_rows':updated,'secret_refs':sorted(set(secret_refs))[:100]}

    def _repair_claim_metadata(self, dry_run: bool=True, limit: int=1000) -> Dict[str, Any]:
        """Heal corrupted lifecycle/topic metadata that breaks dashboards and recall hygiene."""
        c=self._connect(); fixes=[]; limit=max(1,min(int(limit or 1000),5000))
        rows=c.execute("SELECT id,claim,status,topic FROM claims ORDER BY updated_at DESC LIMIT 20000").fetchall()
        for r in rows:
            old_status=str(r['status'] or '')
            old_topic=str(r['topic'] or '')
            new_status=normalize_claim_status(old_status)
            new_topic=old_topic
            topic_reason=topic_integrity_reason(old_topic)
            if topic_reason:
                claim_low=str(r['claim'] or '').lower()
                if claim_low.startswith('hermes env/config metadata') or 'configured variables' in claim_low:
                    new_topic='config'
                else:
                    new_topic=self._topic_alias(self._infer_topic(r['claim']), r['claim'])
            if new_status != old_status or new_topic != old_topic:
                fixes.append({'id':r['id'], 'old_status':old_status, 'new_status':new_status, 'old_topic':old_topic, 'new_topic':new_topic, 'topic_reason':topic_reason})
                if len(fixes) >= limit:
                    break
        if not dry_run and fixes:
            ts=now()
            with c:
                for f in fixes:
                    c.execute('UPDATE claims SET status=?, topic=?, updated_at=?, quality=max(quality, ?) WHERE id=?', (f['new_status'], f['new_topic'], ts, claim_quality(c.execute('SELECT claim FROM claims WHERE id=?',(f['id'],)).fetchone()['claim'], f['new_topic']), f['id']))
                    self._add_change('repair_claim_metadata', f['id'], f"status {f['old_status']} -> {f['new_status']}; topic {f['old_topic']} -> {f['new_topic']}")
            self._rebuild_fts(); self._render_all()
        return {'fix_count':len(fixes), 'fixes':fixes[:50]}

    def _repair(self, target: str='all', dry_run: bool=True) -> Dict[str, Any]:
        target=(target or 'all').lower(); actions=[]
        def act(name, fn=None, run_when_dry=False):
            entry={'action':name,'applied':not dry_run}
            if fn and (run_when_dry or not dry_run):
                result=fn()
                if isinstance(result, dict): entry.update(result)
            actions.append(entry)
        if target in ('all','integrity'):
            act('migrate_schema', self._migrate)
            act('repair_claim_metadata', lambda: self._repair_claim_metadata(dry_run), run_when_dry=True)
        if target in ('all','fts'):
            act('rebuild_claims_fts', self._rebuild_fts)
        if target in ('all','dashboards'):
            act('render_topic_pages', self._render_all); act('render_active_dashboard', self._render_active_dashboard)
        if target in ('all','integrity'):
            def preserve():
                self._preserve_db_files('repair')
            act('preserve_db_files', preserve)
            def optimize():
                c=self._connect(); c.execute('PRAGMA optimize'); self._checkpoint_wal('TRUNCATE')
            act('sqlite_optimize_checkpoint', optimize)
        if not dry_run: self._audit('repair','ok',f'target={target} actions={len(actions)}')
        return {'target':target,'dry_run':dry_run,'actions':actions}

    def _audit_log(self, limit:int=50)->List[Dict[str,Any]]:
        try:
            return [dict(r) for r in self._connect().execute('SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?', (max(1,min(limit,500)),)).fetchall()]
        except Exception:
            return []

    def _snapshot(self, name:str='')->Dict[str,Any]:
        stamp=time.strftime('%Y%m%d_%H%M%S', time.localtime(now()))
        raw=slug(name or ('snapshot_'+stamp))
        fname=(raw if raw.startswith('snapshot_') else f'{stamp}_{raw}')+'.md'
        path=safe_join(self.snapshots_dir, fname)
        d=self._dashboard(20); active=self._active_dashboard(120)['content']
        lines=[f"# Memory Wiki Snapshot {stamp}","",active,"","## Dashboard summary",json.dumps(d,ensure_ascii=False,indent=2)[:6000]]
        atomic_write(path, '\n'.join(lines)+'\n')
        return {'path':str(path)}

    def _export(self, limit=200) -> Dict[str,Any]:
        c=self._connect(); limit=max(1,min(limit,2000))
        clean = self._sanitize_row
        return {"success":True,"claims":[clean(r) for r in c.execute("SELECT * FROM claims ORDER BY updated_at DESC LIMIT ?",(limit,)).fetchall()],"evidence":[clean(r) for r in c.execute("SELECT * FROM evidence ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()],"contradictions":[clean(r) for r in c.execute("SELECT * FROM contradictions ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()],"review_queue":[clean(r) for r in c.execute("SELECT * FROM review_queue ORDER BY updated_at DESC LIMIT ?",(limit,)).fetchall()],"secret_quarantine":[clean(r) for r in c.execute("SELECT * FROM secret_quarantine ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()],"changes":[clean(r) for r in c.execute("SELECT * FROM memory_changes ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()],"mutations":[clean(r) for r in c.execute("SELECT * FROM memory_mutations ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()],"project_profiles":[clean(r) for r in c.execute("SELECT * FROM project_profiles ORDER BY updated_at DESC LIMIT ?",(limit,)).fetchall()],"entities":[clean(r) for r in c.execute("SELECT * FROM entities ORDER BY updated_at DESC LIMIT ?",(limit,)).fetchall()],"relations":[clean(r) for r in c.execute("SELECT * FROM relations ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()],"sync_bundles":[clean(r) for r in c.execute("SELECT * FROM sync_bundles ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()],"audit":[clean(r) for r in c.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()]}


def register(ctx) -> None:
    ctx.register_memory_provider(MemoryWikiProvider())
