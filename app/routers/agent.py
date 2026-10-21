"""LLM agent endpoints (protected by X-API-Key, rate-limited by nginx)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.agent.graph import GraphAgent
from app.agent.llm import build_llm
from app.config import get_settings
from app.database import Neo4jClient
from app.dependencies import get_db, require_api_key
from app.queries.schema import SCHEMA_FOR_LLM

router = APIRouter(prefix="/api/agent", tags=["agent"], dependencies=[Depends(require_api_key)])


class AskRequest(BaseModel):
    """A natural-language question."""

    question: str = Field(..., min_length=3, max_length=1000, examples=["How do I get from LYS to BKK?"])


class ValidateRequest(BaseModel):
    """A raw Cypher statement, as an LLM could have produced it."""

    cypher: str = Field(..., min_length=1, max_length=4000, examples=["MATCH (a:Airport) DETACH DELETE a"])


class GuardrailCheck(BaseModel):
    """One guardrail check result."""

    stage: str
    check: str
    passed: bool
    detail: str = ""


class VerifiedData(BaseModel):
    """Data coming straight from Neo4j (facts), separate from the LLM's wording."""

    source: str
    tool: str | None = None
    cypher: str | None = None
    rows: list[dict]


class AgentResponse(BaseModel):
    """Structured agent answer."""

    question: str
    status: str = Field(..., description="answered | no_results | refused | rejected | failed")
    answer: str
    answer_is_ai_generated: bool
    grounded: bool = Field(..., description="every code/number in `answer` appears in verified_data.rows")
    confidence: float
    verified_data: VerifiedData
    attempts: int
    llm: str
    guardrails: list[GuardrailCheck]
    trace: list[str]


class ValidateResponse(BaseModel):
    """Result of the Cypher guardrails alone."""

    valid: bool
    query: str | None
    checks: list[GuardrailCheck]


def get_agent(request: Request, db: Annotated[Neo4jClient, Depends(get_db)]) -> GraphAgent:
    """Build the agent once per app and cache it on app.state."""
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        settings = get_settings()
        agent = GraphAgent(db, build_llm(settings), settings)
        request.app.state.agent = agent
    return agent


Agent = Annotated[GraphAgent, Depends(get_agent)]


@router.post("/ask", response_model=AgentResponse, summary="Ask a question in natural language")
def ask(body: AskRequest, agent: Agent) -> dict:
    """LangGraph agent: plan (tool or Cypher) -> guardrails -> Neo4j -> grounded answer."""
    return agent.ask(body.question)


@router.post("/validate", response_model=ValidateResponse, summary="Run the Cypher guardrails on a query")
def validate(body: ValidateRequest, agent: Agent) -> dict:
    """Demonstrate how an invalid / malicious LLM output is detected, without any LLM call."""
    return agent.validate_cypher(body.cypher)


@router.get("/schema", summary="Schema shown to the LLM")
def schema() -> dict:
    """The exact schema description used in the planner prompt."""
    return {"schema": SCHEMA_FOR_LLM}
