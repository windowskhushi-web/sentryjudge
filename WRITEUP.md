# SentryJudge — Project Write-up

## Problem solved

Banks, PSPs, and fintechs are deploying LLM support assistants into the most sensitive
conversation in the business: a customer talking about their card. These models are
fluent, confident, and helpful-sounding — which is exactly what makes their failures
hard to catch. Three failure modes matter most: **leakage** (the assistant repeats a
full card number, CVV, or OTP), **unauthorised commitment** (it promises a refund or
settlement timeline no agent can actually authorise), and **confident hallucination**
(it invents a fee, regulation, or processing window that sounds plausible and is
wrong). Traditional QA can't catch these — string matching doesn't understand "I've
reversed the charge," and human review doesn't scale to conversation volume. A single
leaked card number is a PCI DSS incident, not a hypothetical. SentryJudge is an
LLM-as-a-Judge evaluation gate that sits between an AI support assistant and the
customer, judges every generated response against a versioned rubric before it ships,
and — as of this build — can actually intercept and block a response that fails, not
just report on it afterward.

## Approach and design

**Hybrid judging.** A deterministic scanner (Luhn-validated card numbers, CVV/OTP
regex, Aadhaar/PAN/IFSC patterns) and an LLM judge run on every response. The scanner
catches what must never be missed, and its findings are injected into the judge
prompt as established fact — critically, the scanner *overrides* the LLM on the
leakage criterion, so a model having an off day can't silently let a card number
through.

**Weighted, versioned rubric.** Five criteria (data leakage, policy adherence,
factual grounding, relevance, tone), each with explicit weights and written 0–5
scoring anchors. Two criteria are marked critical: either one scoring ≤2 forces a
`BLOCK` regardless of the weighted average, so a strong overall score can never mask
a compliance failure.

**Evidence-first explainability.** The judge must quote the verbatim span of the
response that drove each score, or explicitly return nothing — no invented evidence.

**Inline gateway (the core addition this session).** SentryJudge doesn't just report
anymore — a `BLOCK` verdict is intercepted before it reaches the customer and swapped
for a safe escalation message, live, turning a passive evaluation pipeline into an
active firewall in front of the assistant.

**Multi-format input.** The same five criteria generalise beyond chat — an
input-type system (chat, generated text/documents, code, AI agent output/trace,
API response) reframes the prompt and UI per format, verified live against real
chat, code, and agent-trace examples. A code snippet that logs a CVV in plain text
gets caught by the same `data_leakage` criterion that catches it in a chat reply.

**Provider-agnostic judge.** The primary judge is a config switch
(`JUDGE_PROVIDER=gemini` or `groq`), not a hardcoded dependency, so a quota or
account issue on one provider doesn't take down evaluation — this came directly out
of hitting exactly that problem during the build.

**API + reporting.** A FastAPI service (`api.py`) exposes the same judge and gateway
over HTTP so another system can call it directly. JSON/CSV export covers single
evaluations, batch runs, and full history.

## Technologies used

Python, Streamlit (console UI), FastAPI + Uvicorn (API), SQLite (evaluation
history), Google Gemini and Groq/Llama (interchangeable LLM judge providers),
pandas (dashboard aggregation), regex + Luhn checksum (deterministic PII/PAN
scanner). Deployed on Streamlit Community Cloud, source on GitHub.

## Challenges faced

The main technical challenge wasn't the rubric or the LLM prompting — it was making
the judge actually reliable enough to trust for a compliance decision. An LLM alone
can miss a card number it's seen a thousand masked variants of; solving that meant
not trusting the LLM alone, and building a deterministic scanner that overrides it
specifically on the one criterion where "usually right" isn't good enough.

Separately, mid-build the primary LLM provider (Gemini) started rejecting
authentication at the account level for reasons outside the code's control. Rather
than block on debugging a third party's account policy, the judge was refactored to
be provider-agnostic (Gemini or Groq behind one interface), which turned an outage
into a config change — and is now a legitimate resilience feature rather than a
workaround. A second, quieter bug surfaced during that fix: environment variables
were being read as module-level constants at import time, before `.env` had actually
loaded, because of import ordering — a good reminder that "it works when I test it
standalone" and "it works inside the actual app's import graph" are different
claims.
