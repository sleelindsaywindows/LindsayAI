"""
Two-agent NL order parsing pipeline.

Agent 1 (parser):   converts free-text input → structured JSON order
Agent 2 (verifier): independently re-reads original text + parsed result, flags issues

The verifier never shares context with the parser, so it catches hallucinated values.
"""

import json
import os
from anthropic import Anthropic
from .models import Order

_client = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


_PARSE_SYSTEM = """You extract order data from natural language input for a window manufacturing delivery planner.
Return ONLY a valid JSON object — no explanation, no markdown.

Fields:
- order_id: string (use what's given; generate "ORD-XXX" if absent)
- customer_name: string
- address: string (full ship-to address as stated — this is the delivery destination)
- capacity_units: number (floor space this order needs in square feet)
- priority: integer 0-10 (0=normal, 10=urgent; infer from words like "rush", "urgent", "ASAP")
- notes: string (driver instructions, gate codes, dock info, or empty string)"""

_VERIFY_SYSTEM = """You are a quality-check agent for a window delivery planning system.
Given the original user input and a parsed order, verify the parse is accurate.
Return ONLY a valid JSON object — no explanation, no markdown.

Fields:
- confident: boolean
- issues: array of strings (specific problems found; empty array if none)
- summary: string (one-sentence confirmation or correction for the user)"""


def _parse_order(text: str, unit_label: str) -> dict:
    resp = _get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=_PARSE_SYSTEM,
        messages=[{"role": "user", "content": f"Unit of measurement: {unit_label}\n\nInput: {text}"}],
    )
    return json.loads(resp.content[0].text)


def _verify_parse(original_text: str, parsed: dict, unit_label: str) -> dict:
    payload = (
        f"Unit of measurement: {unit_label}\n\n"
        f"Original input: {original_text}\n\n"
        f"Parsed result:\n{json.dumps(parsed, indent=2)}"
    )
    resp = _get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=_VERIFY_SYSTEM,
        messages=[{"role": "user", "content": payload}],
    )
    return json.loads(resp.content[0].text)


def parse_and_verify(text: str, unit_label: str) -> tuple[dict, dict]:
    """Returns (parsed_order_dict, verification_result_dict)."""
    parsed = _parse_order(text, unit_label)
    verification = _verify_parse(text, parsed, unit_label)
    return parsed, verification


def dict_to_order(d: dict) -> Order:
    return Order(
        order_id=str(d["order_id"]),
        customer_name=d["customer_name"],
        address=d["address"],
        capacity_units=float(d["capacity_units"]),
        priority=int(d.get("priority", 0)),
        notes=d.get("notes", ""),
    )
