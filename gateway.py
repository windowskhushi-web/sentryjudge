"""
gateway.py — inline enforcement layer.

judge.py only ever reports. This module is what turns that report into an
action: a BLOCK verdict never reaches the customer. The response that was
actually drafted and the response actually delivered can now differ, and
that gap is the whole point — it's the difference between an evaluation
pipeline and a firewall.

Kept to one function on purpose: this layer is trusted to make exactly one
decision (deliver the draft, or deliver the fallback). Any judgement beyond
that belongs in judge.py, not here.
"""

DEFAULT_FALLBACK = (
    "I'm not able to complete that automatically. I've escalated this "
    "conversation to a specialist, who will follow up shortly."
)

# The delivered fallback should read naturally for what actually got blocked —
# a code diff isn't a "conversation," an agent action isn't a "customer."
FALLBACK_BY_INPUT_TYPE = {
    "chat": DEFAULT_FALLBACK,
    "text": "This content was blocked by an automated policy check and held for manual review before publishing.",
    "code": "This code change was blocked by an automated policy check and requires manual review before merge.",
    "agent": "This agent action was blocked by policy and queued for human approval before it runs.",
    "api": "This response was withheld by policy. The caller received a generic error instead of the underlying payload.",
}


def guard(ai_response: str, result: dict, input_type: str = "chat", fallback: str | None = None) -> dict:
    """Decide what the customer actually receives, given a judge verdict."""
    intercepted = result["verdict"] == "BLOCK"
    if fallback is None:
        fallback = FALLBACK_BY_INPUT_TYPE.get(input_type, DEFAULT_FALLBACK)
    return {
        "delivered_response": fallback if intercepted else ai_response,
        "intercepted": intercepted,
        "fallback_reason": result["verdict_reason"] if intercepted else None,
    }
