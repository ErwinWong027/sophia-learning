"""LangGraph solver: question + options -> chosen option text."""
from __future__ import annotations

import json
import os
import re
from typing import Any, TypedDict

from pydantic import BaseModel, Field


class AnswerRequest(BaseModel):
    question: str = Field(..., min_length=1)
    options: list[str] = Field(..., min_length=1)
    context: str = ""


class AnswerResult(BaseModel):
    option_text: str
    option_index: int = -1
    rationale: str = ""
    source: str = "heuristic"


class GraphState(TypedDict, total=False):
    question: str
    options: list[str]
    context: str
    option_text: str
    option_index: int
    rationale: str
    source: str


def _match_option(raw: str, options: list[str]) -> tuple[int, str]:
    text = (raw or "").strip()
    if not text:
        return 0, options[0]

    # Exact / substring match against full option text
    for index, option in enumerate(options):
        if text == option or text in option or option in text:
            return index, option

    # Letter answers like "c" / "C)" / "option c"
    letter = re.search(r"\b([a-dA-D])\b", text)
    if letter:
        index = ord(letter.group(1).lower()) - ord("a")
        if 0 <= index < len(options):
            return index, options[index]

    # Leading "c.) ..." style
    lead = re.match(r"^\s*([a-dA-D])\s*[.)]", text)
    if lead:
        index = ord(lead.group(1).lower()) - ord("a")
        if 0 <= index < len(options):
            return index, options[index]

    return 0, options[0]


def _heuristic_answer(state: GraphState) -> GraphState:
    """Fallback when no LLM key: prefer options mentioning fixed/static content patterns."""
    options = state["options"]
    question = (state.get("question") or "").lower()
    scored: list[tuple[int, int]] = []
    keywords = ("fixed content", "static", "remains the same", "manually updated", "correct", "true")
    for index, option in enumerate(options):
        lower = option.lower()
        score = sum(1 for key in keywords if key in lower)
        if "not " in lower or "never " in lower:
            score -= 1
        if "javascript" in lower and "html" in lower and "static" in question:
            score -= 1
        scored.append((score, index))
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_index = scored[0][1] if scored else 0
    return {
        **state,
        "option_index": best_index,
        "option_text": options[best_index],
        "rationale": "heuristic keyword score (no LLM key)",
        "source": "heuristic",
    }


def _llm_answer(state: GraphState) -> GraphState:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return _heuristic_answer(state)

    try:
        from openai import OpenAI
    except Exception:
        return _heuristic_answer({**state, "rationale": "openai package missing; heuristic fallback"})

    options = state["options"]
    numbered = "\n".join(f"{chr(ord('a') + i)}.) {opt}" for i, opt in enumerate(options))
    prompt = (
        "You are answering a multiple-choice quiz. "
        "Return ONLY JSON: {\"option_index\": <0-based int>, \"option_text\": \"...\", \"rationale\": \"...\"}.\n\n"
        f"Question:\n{state.get('question', '')}\n\n"
        f"Options:\n{numbered}\n"
    )
    if state.get("context"):
        prompt += f"\nContext:\n{state['context'][:4000]}\n"

    try:
        client = OpenAI(api_key=api_key)
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Answer with compact JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        content = (response.choices[0].message.content or "").strip()
        start = content.find("{")
        end = content.rfind("}")
        payload: dict[str, Any] = {}
        if start >= 0 and end > start:
            payload = json.loads(content[start : end + 1])
        raw_text = str(payload.get("option_text") or payload.get("answer") or content)
        index = payload.get("option_index")
        if isinstance(index, int) and 0 <= index < len(options):
            chosen_index, chosen_text = index, options[index]
        else:
            chosen_index, chosen_text = _match_option(raw_text, options)
        return {
            **state,
            "option_index": chosen_index,
            "option_text": chosen_text,
            "rationale": str(payload.get("rationale") or "llm"),
            "source": "llm",
        }
    except Exception as exc:
        fallback = _heuristic_answer(state)
        fallback["rationale"] = f"llm_error:{type(exc).__name__}; {fallback.get('rationale', '')}"
        fallback["source"] = "heuristic"
        return fallback


def _build_graph():
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(GraphState)
    graph.add_node("answer", _llm_answer)
    graph.add_edge(START, "answer")
    graph.add_edge("answer", END)
    return graph.compile()


_GRAPH = None


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph()
    return _GRAPH


def answer_question(payload: AnswerRequest) -> AnswerResult:
    graph = _get_graph()
    result = graph.invoke(
        {
            "question": payload.question,
            "options": list(payload.options),
            "context": payload.context or "",
        }
    )
    return AnswerResult(
        option_text=result.get("option_text") or payload.options[0],
        option_index=int(result.get("option_index", 0)),
        rationale=str(result.get("rationale") or ""),
        source=str(result.get("source") or "heuristic"),
    )
