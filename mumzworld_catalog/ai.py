"""Optional cataloging assistant using Claude.

Switched off by default (Settings).  When on, it is asked only for judgment
calls: choosing among approved categories, finding an approved dropdown value
equivalent to a vendor value, drafting content from the supplied evidence, and
describing product images.  Its answers are constrained to approved values,
checked again here, and always recorded as "Cataloging judgment" needing
review.  If the assistant is unavailable, the deterministic rules run alone and
the reason is recorded.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from . import config

SYSTEM = (
    "You are a catalog operations specialist for Mumzworld, an online store for mothers, babies and children. "
    "You make cataloging decisions only from the evidence supplied in the request: vendor data, approved "
    "reference lists, approved rules and SOP excerpts. Never invent specifications, materials, dimensions, "
    "weight limits, age suitability, safety certifications, medical or health benefits. When the evidence does "
    "not support a decision, say so through the 'insufficient' or 'confidence' fields instead of guessing. "
    "Choose values only from the approved lists you are given."
)


class AssistantError(RuntimeError):
    pass


class Assistant:
    def __init__(self, client=None, model: str | None = None, effort: str | None = None):
        cfg = config.load()
        self.model = model or cfg.get("ai_model") or "claude-opus-5-5"
        self.effort = effort or cfg.get("ai_effort") or "medium"
        if client is None:
            try:
                import anthropic
            except ImportError as e:
                raise AssistantError("The 'anthropic' package is not installed.") from e
            client = anthropic.Anthropic()
        self.client = client

    def _ask(self, content, schema: dict, max_tokens: int = 4000) -> dict:
        try:
            import anthropic
            api_errors = (anthropic.APIError,)
        except ImportError:  # a test double needs no SDK
            api_errors = ()
        try:
            resp = self.client.beta.messages.create(
                model=self.model, max_tokens=max_tokens, system=SYSTEM,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
        except api_errors as e:
            raise AssistantError(f"Assistant request failed: {e}") from e
        if getattr(resp, "stop_reason", "") == "refusal":
            raise AssistantError("The assistant declined this request.")
        if getattr(resp, "stop_reason", "") == "max_tokens":
            raise AssistantError("The assistant's answer was cut off.")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        try:
            return json.loads(text)
        except ValueError as e:
            raise AssistantError("The assistant returned unreadable output.") from e

    # -- decisions ----------------------------------------------------------------------
    def choose_category(self, evidence: dict, candidates: list[str], sop: str = "") -> dict:
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["category", "confidence", "rationale", "insufficient"],
                  "properties": {"category": {"type": "string"},
                                 "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                                 "rationale": {"type": "string"}, "insufficient": {"type": "boolean"}}}
        prompt = ("Pick the single most specific approved category for this product. Answer with one category "
                  "exactly as written in the list, or set insufficient=true if the evidence cannot distinguish "
                  "between them.\n\nPRODUCT EVIDENCE:\n" + json.dumps(evidence, ensure_ascii=False, indent=1)
                  + "\n\nAPPROVED CATEGORIES:\n" + "\n".join(candidates)
                  + (f"\n\nSOP EXCERPT:\n{sop[:20000]}" if sop else ""))
        out = self._ask(prompt, schema)
        if out.get("category") not in candidates:
            out["insufficient"] = True
        return out

    def map_value(self, attribute: str, vendor_value: str, allowed: list[str], evidence: dict) -> dict:
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["values", "equivalent", "rationale"],
                  "properties": {"values": {"type": "array", "items": {"type": "string"}},
                                 "equivalent": {"type": "boolean"}, "rationale": {"type": "string"}}}
        prompt = (f"Attribute: {attribute}\nVendor value: {vendor_value}\n\nIs there an approved value with the "
                  "same meaning? Return the approved value(s) exactly as listed, or equivalent=false when no "
                  "approved value means the same thing. Do not pick a merely similar value.\n\nAPPROVED VALUES:\n"
                  + "\n".join(allowed[:800]) + "\n\nPRODUCT EVIDENCE:\n"
                  + json.dumps(evidence, ensure_ascii=False, indent=1))
        out = self._ask(prompt, schema, 2000)
        out["values"] = [v for v in out.get("values", []) if v in allowed]
        if not out["values"]:
            out["equivalent"] = False
        return out

    def write_content(self, evidence: dict, rules: dict, examples: list[str], sop: str = "") -> dict:
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["title", "short_description", "long_description", "bullet_points", "search_terms",
                               "unsupported_requests"],
                  "properties": {"title": {"type": "string"}, "short_description": {"type": "string"},
                                 "long_description": {"type": "string"},
                                 "bullet_points": {"type": "array", "items": {"type": "string"}},
                                 "search_terms": {"type": "array", "items": {"type": "string"}},
                                 "unsupported_requests": {"type": "array", "items": {"type": "string"}}}}
        prompt = ("Write catalog content for this product in clear, customer-friendly English that matches "
                  "Mumzworld's listings. Use only facts present in the evidence. No unsupported benefits, medical "
                  "or safety claims, certifications or specifications. No keyword stuffing. Follow the content "
                  "rules exactly (title pattern, lengths, capitalization). List in unsupported_requests anything "
                  "the rules ask for that the evidence does not provide.\n\nCONTENT RULES:\n"
                  + json.dumps(rules, ensure_ascii=False, indent=1) + "\n\nPRODUCT EVIDENCE:\n"
                  + json.dumps(evidence, ensure_ascii=False, indent=1)
                  + ("\n\nAPPROVED EXAMPLE TITLES:\n" + "\n".join(examples[:20]) if examples else "")
                  + (f"\n\nSOP EXCERPT:\n{sop[:20000]}" if sop else ""))
        return self._ask(prompt, schema, 6000)

    def inspect_image(self, image_path: Path, media_type: str, evidence: dict) -> dict:
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["product_type", "colours", "visible_features", "packaging", "accessories",
                               "mismatches", "notes"],
                  "properties": {"product_type": {"type": "string"},
                                 "colours": {"type": "array", "items": {"type": "string"}},
                                 "visible_features": {"type": "array", "items": {"type": "string"}},
                                 "packaging": {"type": "string"},
                                 "accessories": {"type": "array", "items": {"type": "string"}},
                                 "mismatches": {"type": "array", "items": {"type": "string"}},
                                 "notes": {"type": "string"}}}
        data = base64.standard_b64encode(Path(image_path).read_bytes()).decode()
        content = [{"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
                   {"type": "text", "text": "Describe only what is visible: product type, colours, visible "
                    "features, packaging and included accessories. List any visible inconsistency with the vendor "
                    "data below. Do not infer materials, dimensions, weight limits or certifications.\n\nVENDOR DATA:\n"
                    + json.dumps(evidence, ensure_ascii=False, indent=1)}]
        return self._ask(content, schema, 2000)


def get():
    """(assistant, "") or (None, reason) when it is switched off or unavailable."""
    cfg = config.load()
    if not cfg.get("ai_enabled"):
        return None, "Assistant switched off in Settings."
    try:
        return Assistant(), ""
    except AssistantError as e:
        return None, str(e)
    except Exception as e:  # missing credentials and similar SDK set-up errors
        return None, f"Assistant unavailable: {e}"
