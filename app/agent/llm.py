"""LLM clients.

* OpenAICompatibleLLM: any provider exposing `/chat/completions` (OpenAI, Groq,
  Mistral, OpenRouter, Ollama's `/v1`...). One small httpx call, no SDK lock-in.
* MockLLM: deterministic rule-based stand-in used by tests and for offline demos.
  It deliberately produces some BAD outputs (a write query, a hallucinated
  property) so the guardrails can be demonstrated without a real model.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

import httpx

from app.config import Settings


class LLMError(Exception):
    """The LLM provider failed or returned something unusable."""


class LLMClient(Protocol):
    """Minimal interface used by the agent."""

    name: str

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        """Return the model's text answer."""


class OpenAICompatibleLLM:
    """Chat-completions client for OpenAI-compatible APIs."""

    def __init__(self, settings: Settings) -> None:
        self.name = f"{settings.llm_provider}:{settings.llm_model}"
        self._settings = settings

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        """Call `/chat/completions` with temperature 0 (reproducible plans)."""
        payload = {
            "model": self._settings.llm_model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._settings.llm_api_key}"} if self._settings.llm_api_key else {}
        url = self._settings.llm_base_url.rstrip("/") + "/chat/completions"
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=self._settings.llm_timeout_s)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"] or ""
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"LLM call failed: {exc}") from exc


class MockLLM:
    """Deterministic fake LLM (offline tests and demos)."""

    name = "mock"

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        """Plan with simple rules, or summarise the first rows verbatim."""
        _ = system
        if json_mode:
            return json.dumps(self._plan(user))
        return self._summarise(user)

    @staticmethod
    def _plan(prompt: str) -> dict:
        retry = "previous plan was rejected" in prompt.lower()
        question = prompt.split("Question:", 1)[-1]
        lower = question.lower()
        codes = re.findall(r"\b[A-Z]{3}\b", question)
        for rule in (_unsafe_rule, _hallucination_rule, _tool_rule, _country_rule):
            plan = rule(question, lower, codes, retry)
            if plan:
                return plan
        return {"action": "cypher", "cypher": "MATCH (a:Airport) RETURN count(a) AS airports", "reasoning": "mock"}

    @staticmethod
    def _summarise(prompt: str) -> str:
        match = re.search(r"Results \([^)]*\):\n(.*)$", prompt, re.DOTALL)
        try:
            rows = json.loads(match.group(1)) if match else None
        except ValueError:
            rows = None
        if rows is None:
            return "The query returned results (mock summary)."
        if not rows:
            return "No matching data was found."
        return f"The graph returned {len(rows)} row(s). First result: {json.dumps(rows[0])}"


# --- MockLLM planning rules: each returns a plan or None --------------------
def _unsafe_rule(_question: str, lower: str, _codes: list, _retry: bool) -> dict | None:
    """Simulate a model obeying a destructive request (guardrail demo)."""
    if re.search(r"\b(delete|drop|remove|wipe)\b", lower):
        return {"action": "cypher", "cypher": "MATCH (a:Airport) DETACH DELETE a", "reasoning": "mock: unsafe"}
    return None


def _hallucination_rule(_question: str, lower: str, codes: list, retry: bool) -> dict | None:
    """Simulate a hallucinated property, then a refusal once the validator complains."""
    if "population" not in lower:
        return None
    if retry:
        return {"action": "refuse", "reasoning": "population is not stored in the graph"}
    code = codes[0] if codes else "CDG"
    return {"action": "cypher", "cypher": f"MATCH (a:Airport {{iata: '{code}'}}) RETURN a.population AS population",
            "reasoning": "mock: hallucinated property"}


def _tool_rule(_question: str, lower: str, codes: list, _retry: bool) -> dict | None:
    """Pick a vetted tool from keywords and IATA codes."""
    if len(codes) >= 2:
        tool, args = "shortest_route", {"source": codes[0], "target": codes[1]}
    elif codes and re.search(r"reach|within|hops?|connections?", lower):
        tool, args = "reachable", {"iata": codes[0], "hops": int((re.findall(r"\b([1-4])\b", lower) or ["2"])[0])}
    elif re.search(r"busiest|most (connected|central|important)|hubs?\b", lower):
        tool, args = "central_airports", {"metric": "degree", "limit": 5}
    elif codes:
        tool, args = "airport_info", {"iata": codes[0]}
    else:
        return None
    return {"action": "tool", "tool": tool, "args": args, "reasoning": f"mock: {tool}"}


def _country_rule(question: str, _lower: str, _codes: list, _retry: bool) -> dict | None:
    """Count airports in a capitalised country name ("... in Japan")."""
    country = re.search(r"\bin ([A-Z][a-z]+(?: [A-Z][a-z]+)*)", question)
    if not country:
        return None
    return {"action": "cypher", "reasoning": "mock: count by country",
            "cypher": "MATCH (a:Airport)-[:IN_COUNTRY]->(:Country {name: '" + country.group(1)
                      + "'}) RETURN count(a) AS airports"}


def build_llm(settings: Settings) -> LLMClient:
    """Factory driven by LLM_PROVIDER."""
    if settings.llm_provider.lower() == "mock":
        return MockLLM()
    return OpenAICompatibleLLM(settings)


def parse_json_object(text: str) -> dict:
    """Extract the first JSON object from a model answer (tolerates fences / prose)."""
    text = text.strip()
    try:
        value = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise LLMError("LLM did not return a JSON object") from None
        try:
            value = json.loads(match.group(0))
        except ValueError as exc:
            raise LLMError(f"LLM returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMError("LLM JSON is not an object")
    return value
