"""
db.py — evaluation history.

One table, one row per evaluation. Storing the rubric version alongside every
row is what makes prompt-regression analysis possible later: you can compare
scores for the same inputs across rubric versions.

The response text is stored redacted — the whole point of the leakage detector
is undermined if the tool itself keeps a plain-text copy of the PAN.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "evaluations.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    source          TEXT NOT NULL,          -- 'live' or 'batch'
    input_type      TEXT NOT NULL DEFAULT 'chat',
    rubric_version  TEXT NOT NULL,
    model           TEXT NOT NULL,
    user_query      TEXT,
    ai_response     TEXT,                   -- redacted before storage
    verdict         TEXT NOT NULL,
    verdict_reason  TEXT,
    weighted_score  REAL NOT NULL,
    confidence      REAL,
    criteria_json   TEXT NOT NULL,
    findings_json   TEXT NOT NULL,
    summary         TEXT,
    recommendation  TEXT,
    latency_ms      INTEGER
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def redact(text: str, findings: list[dict]) -> str:
    """Replace anything the scanner flagged before it reaches disk."""
    import detectors

    out = detectors.PAN_RE.sub(lambda m: "[REDACTED-PAN]"
                               if detectors.luhn_valid("".join(filter(str.isdigit, m.group())))
                               else m.group(), text)
    out = detectors.CVV_RE.sub("CVV [REDACTED]", out)
    out = detectors.OTP_RE.sub("OTP [REDACTED]", out)
    out = detectors.AADHAAR_RE.sub("[REDACTED-ID]", out)
    out = detectors.EMAIL_RE.sub("[REDACTED-EMAIL]", out)
    return out


def save(result: dict, user_query: str, ai_response: str, source: str = "live") -> int:
    conn = connect()
    with conn:
        cur = conn.execute(
            """INSERT INTO evaluations (
                created_at, source, input_type, rubric_version, model, user_query, ai_response,
                verdict, verdict_reason, weighted_score, confidence,
                criteria_json, findings_json, summary, recommendation, latency_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(timespec="seconds"),
                source,
                result.get("input_type", "chat"),
                result["rubric_version"],
                result["model"],
                user_query[:2000],
                redact(ai_response, result["findings"])[:4000],
                result["verdict"],
                result["verdict_reason"],
                result["weighted_score"],
                result.get("confidence"),
                json.dumps(result["criteria"]),
                json.dumps(result["findings"]),
                result.get("summary", ""),
                result.get("recommendation", ""),
                result.get("latency_ms"),
            ),
        )
    conn.close()
    return cur.lastrowid


def history(limit: int = 500) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM evaluations ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["criteria"] = json.loads(d.pop("criteria_json"))
        d["findings"] = json.loads(d.pop("findings_json"))
        out.append(d)
    return out


def clear() -> None:
    conn = connect()
    with conn:
        conn.execute("DELETE FROM evaluations")
    conn.close()
