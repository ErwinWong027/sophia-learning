from __future__ import annotations

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from solver import AnswerRequest, AnswerResult, answer_question
from web_agent import BrowserAction, BrowserAgent, BrowserAgentConfig


app = FastAPI(title="Sophia Browser Control Agent", version="1.0.0")
agent = BrowserAgent()


class ActionIn(BaseModel):
    operation: Literal[
        "goto", "click", "double_click", "fill", "type", "press", "hover",
        "check", "uncheck", "select", "wait_for", "wait", "extract_text",
        "extract_attribute", "extract_sophia_question", "screenshot",
    ]
    selector: str = ""
    value: str = ""
    url: str = ""
    key: str = ""
    name: str = ""
    attribute: str = ""
    timeout_ms: int = Field(10_000, ge=1, le=120_000)
    delay_ms: int = Field(0, ge=0, le=5_000)


class RunIn(BaseModel):
    target_url: str = Field(..., min_length=1)
    actions: list[ActionIn] = Field(default_factory=list)
    headless: bool = False
    user_data_dir: str = "./browser_profiles/sophia"
    keep_open: bool = False
    default_timeout_ms: int = Field(10_000, ge=1, le=120_000)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/solver/answer", response_model=AnswerResult)
def solver_answer(payload: AnswerRequest) -> AnswerResult:
    """LangGraph quiz solver: question + options -> chosen option."""
    return answer_question(payload)


@app.post("/agent/run")
def run_agent(payload: RunIn) -> dict:
    actions = [BrowserAction(**item.dict()) for item in payload.actions]
    config = BrowserAgentConfig(
        target_url=payload.target_url,
        actions=actions,
        headless=payload.headless,
        user_data_dir=payload.user_data_dir,
        keep_open=payload.keep_open,
        default_timeout_ms=payload.default_timeout_ms,
    )
    return agent.run(config)


@app.get("/agent/status")
def agent_status() -> dict:
    return agent.status()


@app.post("/agent/stop")
def stop_agent() -> dict:
    return agent.stop()
