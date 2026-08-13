"""LangGraph solver: question + options -> chosen option text."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, TypedDict

from pydantic import BaseModel, Field

_ENV_LOADED = False


def _load_dotenv() -> None:
    """Load repo-root .env into os.environ (does not override existing vars)."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


class AnswerRequest(BaseModel):
    question: str = Field(..., min_length=1)
    options: list[str] = Field(..., min_length=1)
    context: str = ""
    images: list[str] = Field(default_factory=list)


class AnswerResult(BaseModel):
    option_text: str
    option_index: int = -1
    rationale: str = ""
    source: str = "heuristic"


class GraphState(TypedDict, total=False):
    question: str
    options: list[str]
    context: str
    images: list[str]
    option_text: str
    option_index: int
    rationale: str
    source: str


def _clean_option_label(option: str) -> str:
    return re.sub(r"^\s*[a-dA-D]\s*[.)]\s*", "", (option or "").strip(), count=1)


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


def _rationale_option_conflict(rationale: str, index: int) -> int | None:
    """检查 rationale 中是否提到与所选不同的选项字母（如 'option c is correct'），
    若与 option_index 矛盾则返回 rationale 支持的选项下标，否则返回 None。"""
    if not rationale:
        return None
    letters = re.findall(r"option\s*([a-dA-D])\b", rationale.lower())
    if not letters:
        return None
    # 取最后一个被正面提及的选项字母（结论通常在末尾）
    last = letters[-1]
    alt_index = ord(last) - ord("a")
    return alt_index if alt_index != index else None


def _parse_llm_payload(content: str, options: list[str]) -> dict[str, Any] | None:
    """解析 LLM 返回的 JSON；解析失败时用正则尽力提取字段。"""
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            pass
    # 正则后备：LLM 回显长题干时容易漏转义，直接抽字段
    payload: dict[str, Any] = {}
    m_idx = re.search(r'"option_index"\s*:\s*(\d+)', content)
    if m_idx:
        payload["option_index"] = int(m_idx.group(1))
    m_txt = re.search(r'"option_text"\s*:\s*"((?:[^"\\]|\\.)*)"', content)
    if m_txt:
        payload["option_text"] = m_txt.group(1)
    m_rat = re.search(r'"rationale"\s*:\s*"((?:[^"\\]|\\.)*)"', content)
    if m_rat:
        payload["rationale"] = m_rat.group(1)
    return payload or None


def _log_llm_request(model: str, messages: list[dict], content: str, attempt: int, parse_ok: bool) -> None:
    """把每次发给 LLM 的 messages 和返回追加写入 logs/llm_requests.jsonl，便于事后审查 prompt。"""
    try:
        import datetime

        log_path = Path(__file__).resolve().parent.parent / "logs" / "llm_requests.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "model": model,
            "attempt": attempt,
            "parse_ok": parse_ok,
            "messages": messages,
            "response": content,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志失败不影响答题


def _llm_answer(state: GraphState) -> GraphState:
    _load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return _heuristic_answer(state)

    try:
        from openai import OpenAI
    except Exception:
        return _heuristic_answer({**state, "rationale": "openai package missing; heuristic fallback"})

    options = state["options"]
    numbered = "\n".join(
        f"{chr(ord('a') + i)}.) {_clean_option_label(opt)}" for i, opt in enumerate(options)
    )
    prompt = (
        "You are answering a Sophia Learning multiple-choice quiz. "
        "Pick the single best answer. "
        "First solve the problem yourself, then eliminate distractors. "
        "If several options are technically true, choose the one stating the most direct root cause "
        "(the most specific reason, not a restatement of a general criterion). "
        "Return ONLY a compact JSON object: {\"option_index\": <0-based int>, \"option_text\": \"...\", \"rationale\": \"brief\"}. "
        "The rationale must explain why the chosen option is better than the closest competitor. "
        "Do NOT repeat the question text; keep rationale under 60 words; escape quotes properly.\n\n"
        f"Question:\n{state.get('question', '')}\n\n"
        f"Options:\n{numbered}\n"
    )
    if state.get("context"):
        prompt += f"\nContext:\n{state['context'][:4000]}\n"

    # 只用纯文本 prompt（图片 alt 描述已包含在 question 中）
    api_messages = [
        {"role": "system", "content": "Answer with compact JSON only. No markdown fences."},
        {"role": "user", "content": prompt},
    ]

    try:
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
        model = os.getenv("OPENAI_MODEL", "deepseek-v4-pro")
        # 最多重试 2 次（首次 + 1 次针对非法 JSON 的重试）
        payload: dict[str, Any] = {}
        content = ""
        messages = api_messages
        for attempt in range(2):
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0,
                stream=False,
            )
            content = (response.choices[0].message.content or "").strip()
            parsed = _parse_llm_payload(content, options)
            _log_llm_request(model, messages, content, attempt, parsed is not None)
            if parsed is not None:
                payload = parsed
                break
            # 非法 JSON：追加严格指令重试一次
            messages = api_messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "Your previous reply was not valid JSON. Reply again with ONLY the compact JSON object."},
            ]
        raw_text = str(payload.get("option_text") or payload.get("answer") or content)
        index = payload.get("option_index")
        if isinstance(index, int) and 0 <= index < len(options):
            chosen_index, chosen_text = index, options[index]
            # 一致性兜底：rationale 明确支持另一个选项时纠正（模型推理对但 index 填错）
            alt_index = _rationale_option_conflict(str(payload.get("rationale") or ""), index)
            if alt_index is not None and 0 <= alt_index < len(options):
                chosen_index, chosen_text = alt_index, options[alt_index]
                payload["rationale"] = (
                    f"{payload.get('rationale', '')} [corrected option_index {index}->{alt_index} by rationale check]"
                )
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
        fallback["rationale"] = f"llm_error:{type(exc).__name__}:{exc}; {fallback.get('rationale', '')}"
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
            "images": list(payload.images or []),
        }
    )
    return AnswerResult(
        option_text=result.get("option_text") or payload.options[0],
        option_index=int(result.get("option_index", 0)),
        rationale=str(result.get("rationale") or ""),
        source=str(result.get("source") or "heuristic"),
    )
