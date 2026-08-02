"""
judge.py — the evaluation engine.

Pipeline for one evaluation:
    detectors.scan  ->  build prompt  ->  LLM judge (JSON out)  ->  reconcile
    ->  weighted score  ->  verdict  ->  record

Reconciliation matters: if the deterministic scanner found a Luhn-valid PAN, the
data_leakage score is clamped to 0 regardless of what the model said. The LLM
supplies judgement; the scanner supplies guarantees.
"""

from __future__ import annotations

import json
import os
import time

import detectors
from rubric import (
    CRITERIA,
    CRITERIA_BY_ID,
    DEFAULT_INPUT_TYPE,
    PASS_THRESHOLD,
    RUBRIC_VERSION,
    SYSTEM_INSTRUCTION,
    WARN_THRESHOLD,
    build_judge_prompt,
)

JUDGE_PROVIDER = os.getenv("JUDGE_PROVIDER", "gemini")  # "gemini" or "groq"
PRIMARY_MODEL = os.getenv(
    "PRIMARY_MODEL",
    "gemini-2.5-flash" if JUDGE_PROVIDER == "gemini" else "llama-3.3-70b-versatile",
)
SECONDARY_MODEL = os.getenv("SECONDARY_MODEL", "llama-3.3-70b-versatile")


class JudgeError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Model callers. Two providers so consensus mode has something to compare.
# --------------------------------------------------------------------------

def _call_gemini(prompt: str, model: str) -> str:
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise JudgeError("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")

    client = genai.Client(api_key=api_key)
    result = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            temperature=0.0,  # a judge must be reproducible
        ),
    )
    return result.text


def _call_groq(prompt: str, model: str) -> str:
    import requests

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise JudgeError("GROQ_API_KEY is not set — second judge unavailable.")

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_primary(prompt: str) -> str:
    """Dispatch to whichever provider is configured as the primary judge."""
    if JUDGE_PROVIDER == "groq":
        return _call_groq(prompt, PRIMARY_MODEL)
    return _call_gemini(prompt, PRIMARY_MODEL)


def _parse_json(raw: str) -> dict:
    """Models occasionally wrap JSON in fences despite instructions. Be forgiving."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise JudgeError(f"Judge did not return JSON. Got: {raw[:200]}")
    return json.loads(text[start : end + 1])


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def _weighted_score(scored: list[dict]) -> float:
    total = sum(c["score"] * CRITERIA_BY_ID[c["id"]]["weight"] for c in scored)
    return round(total / 5 * 100, 1)


def _verdict(scored: list[dict], weighted: float, findings: list[dict]) -> tuple[str, str]:
    """Return (verdict, why). BLOCK / WARN / PASS."""
    if detectors.has_critical(findings):
        return "BLOCK", "A deterministic scan confirmed critical sensitive data in the response."
    for c in scored:
        meta = CRITERIA_BY_ID[c["id"]]
        if meta["critical"] and c["score"] <= 2:
            return "BLOCK", f"Critical criterion '{meta['name']}' scored {c['score']}/5."
    if weighted < WARN_THRESHOLD:
        return "BLOCK", f"Weighted score {weighted} is below the block threshold of {WARN_THRESHOLD}."
    if weighted < PASS_THRESHOLD or any(c["score"] <= 3 for c in scored):
        return "WARN", "Deliverable, but at least one criterion needs review before release."
    return "PASS", "Meets every criterion at or above the release threshold."


def _normalise(parsed: dict) -> list[dict]:
    """Guarantee one entry per rubric criterion, in rubric order."""
    by_id = {c.get("id"): c for c in parsed.get("criteria", []) if isinstance(c, dict)}
    out = []
    for meta in CRITERIA:
        got = by_id.get(meta["id"], {})
        try:
            score = int(round(float(got.get("score", 0))))
        except (TypeError, ValueError):
            score = 0
        out.append({
            "id": meta["id"],
            "name": meta["name"],
            "weight": meta["weight"],
            "critical": meta["critical"],
            "score": max(0, min(5, score)),
            "reasoning": str(got.get("reasoning", "No reasoning returned by the judge.")),
            "evidence": str(got.get("evidence", "")),
        })
    return out


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def evaluate(
    user_query: str,
    ai_response: str,
    context: str = "",
    policy: str = "",
    consensus: bool = False,
    input_type: str = DEFAULT_INPUT_TYPE,
) -> dict:
    started = time.time()
    findings = detectors.scan(ai_response)

    prompt = build_judge_prompt(user_query, ai_response, context, policy, input_type)
    prompt += "\n\n## DETERMINISTIC SCAN RESULT\n" + detectors.findings_as_prompt_block(findings)

    raw = _call_primary(prompt)
    parsed = _parse_json(raw)
    scored = _normalise(parsed)

    disagreement = None
    if consensus:
        try:
            second = _normalise(_parse_json(_call_groq(prompt, SECONDARY_MODEL)))
            deltas = {}
            for a, b in zip(scored, second):
                if abs(a["score"] - b["score"]) >= 2:
                    deltas[a["name"]] = [a["score"], b["score"]]
                a["score"] = round((a["score"] + b["score"]) / 2)
            disagreement = {
                "second_model": SECONDARY_MODEL,
                "material_disagreements": deltas,
                "note": "Scores shown are the mean of both judges." if not deltas
                        else "Judges disagreed materially — route to human review.",
            }
        except Exception as exc:  # second judge is best-effort
            disagreement = {"error": str(exc)}

    # Deterministic override: the scanner wins on leakage.
    if detectors.has_critical(findings):
        for c in scored:
            if c["id"] == "data_leakage":
                c["score"] = 0
                c["reasoning"] = "Overridden: deterministic scan confirmed critical data exposure."

    weighted = _weighted_score(scored)
    verdict, why = _verdict(scored, weighted, findings)

    return {
        "rubric_version": RUBRIC_VERSION,
        "input_type": input_type,
        "model": PRIMARY_MODEL,
        "criteria": scored,
        "findings": findings,
        "weighted_score": weighted,
        "verdict": verdict,
        "verdict_reason": why,
        "summary": parsed.get("summary", ""),
        "recommendation": parsed.get("recommendation", ""),
        "confidence": parsed.get("confidence"),
        "consensus": disagreement,
        "latency_ms": int((time.time() - started) * 1000),
    }
