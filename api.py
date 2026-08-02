"""
api.py — SentryJudge as a service.

The Streamlit console is for a human reviewing evaluations one at a time.
This is the same judge and the same gateway, exposed so another system —
the actual support platform, an agent framework, a CI pipeline reviewing
AI-generated code — can call it directly and get an enforcement decision
back, not just a UI to look at.

Run:
    uvicorn api:app --reload

Then:
    POST /evaluate
    {
      "query": "...", "response": "...", "context": "...", "policy": "...",
      "input_type": "chat"   # optional, defaults to "chat"
    }
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI
from pydantic import BaseModel

import db
import gateway
import judge
from rubric import DEFAULT_INPUT_TYPE, INPUT_TYPES

app = FastAPI(title="SentryJudge API", version="1.0")


class EvaluateRequest(BaseModel):
    query: str = ""
    response: str
    context: str = ""
    policy: str = ""
    input_type: str = DEFAULT_INPUT_TYPE
    consensus: bool = False


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "rubric_version": judge.RUBRIC_VERSION, "provider": judge.JUDGE_PROVIDER}


@app.get("/input-types")
def input_types() -> dict:
    return {k: v["label"] for k, v in INPUT_TYPES.items()}


@app.post("/evaluate")
def evaluate(req: EvaluateRequest) -> dict:
    """Judge one output and return the verdict plus what should actually be delivered."""
    result = judge.evaluate(
        req.query, req.response, req.context, req.policy,
        consensus=req.consensus, input_type=req.input_type,
    )
    db.save(result, req.query, req.response, source="api")
    guard_info = gateway.guard(req.response, result, input_type=req.input_type)
    return {"evaluation": result, "gateway": guard_info}
