"""Continuous Sophia challenge loop:
1. Open browser, wait for manual login if needed.
2. Start from either a question URL or a unit catalog URL.
3. Intra-challenge: extract -> IDE/LLM answer -> select -> Submit -> next question.
4. Inter-challenge: when no new question, click Exit the Challenge -> unit catalog
   -> open the next CHALLENGE item -> resume intra loop.

IDE handoff (default, no API key needed):
  - Writes logs/pending_question.json after extract
  - Waits for logs/pending_answer.json like:
      {"option_text": "fixed content"}  or  {"option_index": 2}
  - Then selects / submits and continues the loop
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from solver import AnswerRequest, answer_question

PROFILE_DIR = Path(__file__).resolve().parent.parent / "browser_profiles" / "sophia"
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
PAUSE_FLAG = LOGS_DIR / "pause.flag"
EXTRACT_FLAG = LOGS_DIR / "extract_only.flag"
AUTOMATION_FLAG = LOGS_DIR / "automation.flag"
MANUAL_FLAG = LOGS_DIR / "manual_answers.flag"


def parse_manual_answers(text: str) -> list[int]:
    """解析手动答案序列：'abcd' / 'a,b,c,d' / 'A B C D' → [0,1,2,3]。"""
    return [ord(c) - ord("a") for c in (text or "").lower() if c in "abcd"]


def rewind_to_first_question(page) -> None:
    """提取模式停在最后一题后，通过左箭头回退到第一题，供答题模式从头开始。"""
    for _ in range(60):
        cur = extract_question(page)
        moved = page.evaluate(
            """() => {
                const arrow = document.querySelector('.left-arrow, a[title="Previous Challenge Question"]');
                if (arrow) { arrow.click(); return true; }
                return false;
            }"""
        )
        if not moved:
            break
        page.wait_for_timeout(700)
        prev = extract_question(page)
        if not prev or (cur and prev.get("question") == cur.get("question")):
            break  # 题目不再变化，已到第一题
    print("REWOUND_TO_FIRST_QUESTION", flush=True)


def check_pause(page=None) -> str:
    """如果 logs/pause.flag 存在，阻塞等待直到文件被删除。浏览器保持打开。
    返回 'normal'（普通恢复）或 'mode_switch'（检测到 EXTRACT_FLAG 模式切换）。
    """
    if not PAUSE_FLAG.exists():
        return "normal"
    print("PAUSED waiting_for_resume", flush=True)
    while PAUSE_FLAG.exists():
        try:
            if page:
                page.wait_for_timeout(1000)
            else:
                time.sleep(1)
        except Exception:
            time.sleep(1)
    # 恢复后检查是否有模式切换信号
    if EXTRACT_FLAG.exists() or AUTOMATION_FLAG.exists() or MANUAL_FLAG.exists():
        print("RESUMED mode_switch_detected", flush=True)
        return "mode_switch"
    print("RESUMED", flush=True)
    return "normal"
QUESTION_PATH = LOGS_DIR / "pending_question.json"
ANSWER_PATH = LOGS_DIR / "pending_answer.json"
DONE_PM_PATH = LOGS_DIR / "done_practice_milestones.json"
DONE_MILESTONE_PATH = LOGS_DIR / "done_milestones.json"
# #region agent log
DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent / ".cursor" / "debug-65171f.log"
# #endregion

LOGIN_MAX_WAIT = 1200
POLL_SECONDS = 3
SUBMIT_WAIT_MS = 12_000
NEXT_QUESTION_WAIT_MS = 20_000
ANSWER_WAIT_MAX = 900

_TIMING = {
    "milestone": {
        "enter_wait_ms": 3000,
        "after_select_wait_ms": 600,
        "submit_wait_ms": 12_000,
        "next_question_wait_ms": 20_000,
    },
    "challenge": {
        "enter_wait_ms": 1500,
        "after_select_wait_ms": 600,
        "submit_wait_ms": 6_000,
        "next_question_wait_ms": 10_000,
    },
}
CHALLENGE_ID_RE = re.compile(r"(challenge-\d+-\d+)", re.I)
ASSESSMENT_ID_RE = re.compile(
    r"((?:practice-)?milestone-\d+-\d+|challenge-\d+-\d+)",
    re.I,
)
CONSENT_MODAL_ID = "sophia-agent-consent-modal"


# #region agent log
def _agent_log(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict | None = None,
    run_id: str = "loop-v1",
) -> None:
    payload = {
        "sessionId": "65171f",
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data or {},
        "timestamp": int(time.time() * 1000),
    }
    line = json.dumps(payload, ensure_ascii=False)
    print(f"DEBUG_LOG {line}", flush=True)
    try:
        DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:
        print(f"DEBUG_LOG_WRITE_FAILED {type(exc).__name__}:{exc}", flush=True)


def _submit_dom_probe(page) -> dict:
    return page.evaluate(
        """() => {
          const buttons = Array.from(document.querySelectorAll('button'));
          const matches = buttons.filter(b => /submit my answer/i.test((b.textContent || '').trim()));
          const pick = matches[0] || null;
          if (!pick) {
            return {
              found: false,
              buttonCount: buttons.length,
              sampleTexts: buttons.slice(0, 8).map(b => (b.textContent || '').trim().slice(0, 80)),
            };
          }
          const style = window.getComputedStyle(pick);
          const rect = pick.getBoundingClientRect();
          return {
            found: true,
            text: (pick.textContent || '').trim(),
            className: pick.className,
            hasDisabledClass: String(pick.className).split(/\\s+/).includes('disabled'),
            disabledAttr: pick.hasAttribute('disabled'),
            disabledProp: !!pick.disabled,
            ariaDisabled: pick.getAttribute('aria-disabled'),
            pointerEvents: style.pointerEvents,
            inViewport: rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < (window.innerHeight || 0),
            rect: {top: rect.top, left: rect.left, width: rect.width, height: rect.height, bottom: rect.bottom},
          };
        }"""
    )


def _selection_dom_probe(page) -> dict:
    return page.evaluate(
        """() => {
          const radios = Array.from(document.querySelectorAll('.question-slide input[type="radio"]'));
          const selectedLike = Array.from(document.querySelectorAll(
            '.question-slide .selected, .question-slide [aria-checked="true"], .question-slide input:checked'
          ));
          return {
            checkedRadios: document.querySelectorAll('.question-slide input:checked').length,
            selectedLikeCount: selectedLike.length,
            radioStates: radios.slice(0, 6).map(r => ({
              id: r.id,
              checked: !!r.checked,
              disabled: !!r.disabled,
            })),
          };
        }"""
    )
# #endregion


def looks_logged_in(page) -> bool:
    """True when not stuck on Sophia sign-in."""
    try:
        url = (page.url or "").lower()
        if "user_sessions/new" in url or "/sign_in" in url:
            return False
        if page.locator("form[action*='user_sessions'] input[type='password']").count() > 0:
            return False
        if page.locator("input[type='password']").count() > 0 and "sophia.org" in url:
            # password field on marketing/login pages only
            if "/home" not in url and "/spcc/" not in url:
                return False
        return True
    except Exception:
        return False


def restore_storage_state(context, storage_state_path: Path) -> dict:
    """Mirror BrowserAgent cookie restore so persistent profile + storage-state both apply."""
    info: dict = {"path": str(storage_state_path), "exists": storage_state_path.exists(), "added": 0}
    if not storage_state_path.exists():
        return info
    try:
        state = json.loads(storage_state_path.read_text(encoding="utf-8"))
        cookies = state.get("cookies", [])
        info["cookie_count"] = len(cookies)
        if cookies:
            context.add_cookies(cookies)
            info["added"] = len(cookies)
            print(f"COOKIES_RESTORED count={len(cookies)}", flush=True)
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}:{exc}"
        print(f"COOKIES_RESTORE_FAILED {info['error']}", flush=True)
    return info


def persist_storage_state(context, storage_state_path: Path, *, reason: str = "") -> bool:
    """Write cookies only when clearly logged in. Safe to call often (login/submit/SIGTERM)."""
    try:
        page_ref = context.pages[0] if context.pages else None
        if not page_ref or not looks_logged_in(page_ref):
            print(
                f"STORAGE_SKIPPED_NOT_LOGGED_IN reason={reason or 'unknown'} "
                f"url={page_ref.url if page_ref else ''}",
                flush=True,
            )
            return False
        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(storage_state_path))
        print(f"STORAGE_SAVED reason={reason or 'manual'} url={page_ref.url}", flush=True)
        return True
    except Exception as exc:
        print(f"STORAGE_SAVE_FAILED reason={reason or 'manual'} {exc}", flush=True)
        return False


def ensure_logged_in(page, target_url: str, context=None, storage_state_path: Path | None = None) -> None:
    """Go straight to challenge URL; only ask for manual login if session never recovers."""
    page.goto(target_url, wait_until="domcontentloaded")
    # Profile cookies sometimes settle after a short redirect bounce through /user_sessions/new.
    soft_wait_s = 12
    waited = 0
    while waited < soft_wait_s and not looks_logged_in(page):
        page.wait_for_timeout(1000)
        waited += 1
    # #region agent log
    _agent_log(
        "LOGIN",
        "login_then_select.py:ensure_logged_in:after_goto",
        "Post-target navigation auth probe",
        {
            "url": page.url,
            "logged_in": looks_logged_in(page),
            "soft_wait_s": waited,
            "password_inputs": page.locator("input[type='password']").count(),
        },
    )
    # #endregion
    if looks_logged_in(page):
        print(f"LOGIN_OK url={page.url}", flush=True)
    else:
        print(f"LOGIN_REQUIRED url={page.url}", flush=True)
        print("请在浏览器完成登录；脚本会等待 cookie 生效后继续。", flush=True)
        while waited < LOGIN_MAX_WAIT and not looks_logged_in(page):
            page.wait_for_timeout(POLL_SECONDS * 1000)
            waited += POLL_SECONDS
            print(f"WAITING_LOGIN... {waited}s url={page.url}", flush=True)
        print("LOGIN_DETECTED" if looks_logged_in(page) else "LOGIN_TIMEOUT", flush=True)

    if not looks_logged_in(page):
        return
    # Persist immediately after auth so a later pkill/crash cannot wipe a good session.
    if context is not None and storage_state_path is not None:
        persist_storage_state(context, storage_state_path, reason="after_login")
    if "user_sessions" in (page.url or "") or target_url.rstrip("/") not in (page.url or ""):
        page.goto(target_url, wait_until="domcontentloaded")
    # Challenge content is a SPA; give it time after auth redirect.
    try:
        page.locator(
            ".question-slide .challenge-v2-question__text, .question-slide .challenge-v2-answer__input"
        ).first.wait_for(state="visible", timeout=15000)
    except Exception:
        page.wait_for_timeout(3000)


def extract_question(page) -> dict | None:
    """Extract question from Challenge or Practice/Milestone pages."""
    # Challenge DOM
    challenge_q = page.locator(".question-slide .challenge-v2-question__text").first
    if challenge_q.count() > 0:
        try:
            challenge_q.wait_for(state="visible", timeout=3000)
        except Exception:
            pass
        else:
            # 一次性提取题干 + 选项（图片 alt 替换为文本）
            data = page.evaluate("""() => {
                const extractWithAlt = (el) => {
                    if (!el) return '';
                    const clone = el.cloneNode(true);
                    clone.querySelectorAll('img[alt]').forEach(img => {
                        const alt = img.alt.trim();
                        if (alt) img.replaceWith(document.createTextNode(alt));
                        else img.remove();
                    });
                    return clone.innerText.replace(/\\s+/g, ' ').trim();
                };
                const slide = document.querySelector('.question-slide');
                // 题干：整个 question-slide 去掉选项/按钮/弹窗区，这样图片块（如 .quiz-zoom-image）也能被捕获
                let question = '';
                if (slide) {
                    const slideClone = slide.cloneNode(true);
                    slideClone.querySelectorAll(
                        '.challenge-v2-answer__input, .challenge-v2-answer, button, nav, input, textarea, select, form, '
                        + '[role=dialog], [role=tooltip], [class*=modal], [class*=dialog], [class*=popover], [class*=tooltip], '
                        + '[class*=report], [class*=feedback], [class*=footer], [class*=controls], [class*=progress]'
                    ).forEach(el => el.remove());
                    question = extractWithAlt(slideClone);
                }
                if (!question) {
                    question = extractWithAlt(document.querySelector('.question-slide .challenge-v2-question__text'));
                }
                // 防御：截断可能残留的反馈弹窗文案
                for (const marker of ['Report an issue', 'Notice something confusing']) {
                    const i = question.indexOf(marker);
                    if (i > 0) question = question.slice(0, i).trim();
                }
                const labels = document.querySelectorAll('.question-slide .challenge-v2-answer__input');
                const options = Array.from(labels).map(label => {
                    const textDiv = label.querySelector('.challenge-v2-answer__text');
                    if (!textDiv) return extractWithAlt(label);
                    // 去掉字母标签 span.letter
                    const clone = textDiv.cloneNode(true);
                    clone.querySelectorAll('span.letter').forEach(el => el.remove());
                    clone.querySelectorAll('img[alt]').forEach(img => {
                        const alt = img.alt.trim();
                        if (alt) img.replaceWith(document.createTextNode(alt));
                        else img.remove();
                    });
                    const text = clone.innerText.replace(/\\s+/g, ' ').trim();
                    return text || extractWithAlt(label);
                });
                return {
                    question: question,
                    options: options,
                };
            }""")
            question_text = data.get("question", "")
            options = data.get("options", [])
            if question_text and options:
                return {
                    "url": page.url,
                    "question": question_text,
                    "options": options,
                    "kind": "challenge",
                    "images": [],
                }

    # Practice Milestone / Milestone DOM (runtime-probed)
    milestone_block = page.locator(".assessment-question-block.multiple_choice, .milestone-take__question-area").first
    if milestone_block.count() == 0:
        return None
    try:
        milestone_block.wait_for(state="visible", timeout=5000)
    except Exception:
        return None

    question_data = page.evaluate(
        """() => {
          const extractWithAlt = (el) => {
            if (!el) return '';
            const clone = el.cloneNode(true);
            clone.querySelectorAll('img[alt]').forEach(img => {
              const alt = img.alt.trim();
              if (alt) img.replaceWith(document.createTextNode(alt));
              else img.remove();
            });
            return clone.innerText.replace(/\\s+/g, ' ').trim();
          };
          const block = document.querySelector('.assessment-question-block.multiple_choice')
            || document.querySelector('.milestone-take__question-area');
          if (!block) return {text: '', options: []};
          // 题干：整个题目块去掉选项区，图片统一转 alt 文本
          const clone = block.cloneNode(true);
          clone.querySelectorAll('ul.answer-fields, .multiple-choice-answer-fields, label.milestone-answer__input, input, button, a').forEach(n => n.remove());
          const heading = (clone.querySelector('h3')?.textContent || '').trim();
          let body = extractWithAlt(clone);
          body = body.replace(/^Question\\s*\\d+/i, '').replace(/Mark this question/i, '').trim();
          // 选项：同样走 alt 提取，避免选项内公式图片丢失
          const labels = block.querySelectorAll('label.milestone-answer__input');
          const options = Array.from(labels).map(label => extractWithAlt(label));
          return {text: body || heading, options: options};
        }"""
    )
    question_text = question_data.get("text", "") if question_data else ""
    options = question_data.get("options", []) if question_data else []
    if not options:
        # 后备：DOM 变体时用 locator 取纯文本
        option_labels = page.locator("label.milestone-answer__input")
        options = [option_labels.nth(i).inner_text().strip() for i in range(option_labels.count())]
    options = [o.strip() for o in options]
    if not question_text or not options:
        return None
    return {
        "url": page.url,
        "question": question_text,
        "options": options,
        "kind": "milestone",
        "images": [],  # 不再提取 base64 图片，只用 alt 文字
    }


def _normalize_option_text(text: str) -> str:
    return " ".join((text or "").split())


def select_option(page, option_text: str, option_index: int | None = None) -> dict:
    """Select an answer on Challenge or Milestone pages."""
    result: dict = {"method": None, "option_index": option_index, "option_text": option_text}

    milestone_labels = page.locator("label.milestone-answer__input")
    if milestone_labels.count() > 0:
        milestone_labels.first.wait_for(state="visible", timeout=10000)
        count = milestone_labels.count()
        result["option_count"] = count
        result["kind"] = "milestone"
        if option_index is not None and 0 <= option_index < count:
            label = page.locator(f'label.milestone-answer__input[for="answer_cb_{option_index}"]')
            if label.count() == 0:
                label = milestone_labels.nth(option_index)
            label.first.click(timeout=8000)
            result["method"] = "milestone_label_for"
            return result
        needle = _normalize_option_text(option_text)
        for i in range(count):
            normalized = _normalize_option_text(milestone_labels.nth(i).inner_text())
            if needle and (needle == normalized or needle in normalized or normalized in needle):
                page.locator(f'label.milestone-answer__input[for="answer_cb_{i}"]').first.click(timeout=8000)
                result["method"] = "milestone_text"
                result["matched_index"] = i
                return result
        raise TimeoutError(f"No milestone option matched text={option_text!r} index={option_index}")

    options = page.locator(".question-slide .challenge-v2-answer__input")
    options.first.wait_for(state="visible", timeout=10000)
    count = options.count()
    result["option_count"] = count
    result["kind"] = "challenge"

    # Sophia: click the LABEL (.challenge-v2-answer__input), not #answer-N radio.
    if option_index is not None and 0 <= option_index < count:
        label = page.locator(f'label.challenge-v2-answer__input[for="answer-{option_index}"]')
        if label.count() > 0:
            label.first.click(timeout=8000)
            result["method"] = "label_for"
            return result
        options.nth(option_index).click(timeout=8000)
        result["method"] = "nth_index"
        return result

    needle = _normalize_option_text(option_text)
    if " " in needle:
        maybe_body = needle.split(" ", 1)[-1].strip()
    else:
        maybe_body = needle

    for i in range(count):
        raw = options.nth(i).inner_text()
        normalized = _normalize_option_text(raw)
        if (
            needle
            and (
                needle == normalized
                or needle in normalized
                or normalized in needle
                or (maybe_body and maybe_body in normalized)
            )
        ):
            label = page.locator(f'label.challenge-v2-answer__input[for="answer-{i}"]')
            if label.count() > 0:
                label.first.click(timeout=8000)
                result["method"] = "text_then_label_for"
            else:
                options.nth(i).click(timeout=8000)
                result["method"] = "text_then_nth"
            result["matched_index"] = i
            result["matched_text"] = normalized[:160]
            return result

    raise TimeoutError(f"No option matched text={option_text!r} index={option_index}")


def wait_for_submit_enabled(page, timeout_ms: int = SUBMIT_WAIT_MS) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        probe = _submit_dom_probe(page)
        if probe.get("found") and not probe.get("hasDisabledClass") and not probe.get("disabledProp"):
            return True
        # Some Sophia builds keep class "disabled" briefly; also accept checked radio + visible button
        selection = _selection_dom_probe(page)
        if probe.get("found") and selection.get("checkedRadios", 0) > 0 and not probe.get("disabledProp"):
            if not probe.get("hasDisabledClass"):
                return True
        page.wait_for_timeout(200)
    return False


def click_submit(page, *, submit_wait_ms: int = SUBMIT_WAIT_MS) -> dict:
    """Click Submit My Answer (challenge) or Save & Continue (milestone)."""
    result: dict = {"attempted": False}
    url_before = page.url
    question_before = None
    extracted = extract_question(page)
    if extracted:
        question_before = extracted.get("question")

    # Milestone / practice-milestone
    milestone_btns = page.locator("button, a").filter(has_text=re.compile(r"Save\s*&\s*Continue", re.I))
    if milestone_btns.count() > 0 and page.locator("label.milestone-answer__input").count() > 0:
        btn = milestone_btns.first
        result["attempted"] = True
        result["kind"] = "milestone"
        try:
            btn.scroll_into_view_if_needed(timeout=3000)
            btn.click(timeout=8000)
            result["click"] = "save_and_continue"
        except Exception as exc:
            result["click"] = f"fail:{type(exc).__name__}:{exc}"
            return result
        page.wait_for_timeout(800)
        # 最后一题：Save & Continue 后 URL 没变，找 Submit 按钮完成最终提交
        # 先轮询等待 URL 变化（最多等 6 秒），避免页面还没加载就误点 Submit
        for _wait_i in range(12):  # 12 × 500ms = 6s
            if page.url != url_before:
                break
            page.wait_for_timeout(500)
        if page.url == url_before:
            # URL 没变：可能是最后一题，也可能是答错了。
            # 不自动点 Submit Milestone，让外层循环自然退出，由用户手动提交。
            result["click"] = "save_and_continue_no_change"
            print("SUBMIT_SKIPPED reason=url_unchanged let_outer_loop_handle", flush=True)
        result["url_before"] = url_before
        result["url_after"] = page.url
        result["question_before"] = question_before
        return result

    buttons = page.locator("button", has_text="Submit My Answer")
    if buttons.count() == 0:
        result["reason"] = "button_not_found"
        return result
    btn = buttons.first

    result["attempted"] = True
    result["kind"] = "challenge"
    result["before"] = _submit_dom_probe(page)
    result["selection"] = _selection_dom_probe(page)

    try:
        btn.scroll_into_view_if_needed(timeout=3000)
        result["scrolled"] = True
    except Exception as exc:
        result["scrolled"] = f"fail:{type(exc).__name__}"

    enabled = wait_for_submit_enabled(page, timeout_ms=submit_wait_ms)
    result["enabled_wait"] = enabled
    result["before_click"] = _submit_dom_probe(page)

    click_mode = ""
    try:
        enabled_buttons = page.locator("button.f-button.blue:not(.disabled)", has_text="Submit My Answer")
        if enabled_buttons.count() > 0:
            enabled_buttons.first.click(timeout=5000)
            click_mode = "enabled_selector"
        else:
            btn.click(timeout=5000)
            click_mode = "text_selector"
    except Exception as click_exc:
        result["normal_click"] = f"fail:{type(click_exc).__name__}:{click_exc}"
        try:
            btn.click(timeout=5000, force=True)
            click_mode = "force"
        except Exception as force_exc:
            result["force_click"] = f"fail:{type(force_exc).__name__}:{force_exc}"
            js_clicked = page.evaluate(
                """() => {
                  const buttons = Array.from(document.querySelectorAll('button'));
                  const pick = buttons.find(b => /submit my answer/i.test((b.textContent || '').trim()));
                  if (!pick) return false;
                  pick.click();
                  return true;
                }"""
            )
            if js_clicked:
                click_mode = "js_click"
            else:
                result["click"] = "fail:all_methods"
                return result

    result["click"] = click_mode
    page.wait_for_timeout(800)
    result["after"] = _submit_dom_probe(page)
    result["url_before"] = url_before
    result["url_after"] = page.url
    result["url_changed"] = page.url != url_before
    result["question_before"] = question_before
    return result


def click_continue_if_present(page) -> str | None:
    candidates = [
        page.locator("button", has_text="Go to the Next Concept"),
        page.locator("a", has_text="Go to the Next Concept"),
        page.locator("button", has_text="Go to the Next Question"),
        page.locator("a", has_text="Go to the Next Question"),
        page.locator("button", has_text="Save & Continue"),
        page.locator("button", has_text="Continue"),
        page.locator("button", has_text="Next"),
        page.locator("button", has_text="Try Again"),
        page.locator("a", has_text="Continue"),
    ]
    for locator in candidates:
        try:
            if locator.count() > 0 and locator.first.is_visible():
                text = locator.first.inner_text().strip()
                # Avoid clicking Exit / unrelated Next controls.
                if "exit the challenge" in text.lower():
                    continue
                locator.first.click(timeout=4000)
                page.wait_for_timeout(800)
                return text
        except Exception:
            continue
    return None


def wait_for_next_question(page, previous_question: str, timeout_ms: int = NEXT_QUESTION_WAIT_MS) -> dict | None:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        clicked = click_continue_if_present(page)
        current = extract_question(page)
        if current and current.get("question") and current["question"] != previous_question:
            current["advanced_via"] = clicked or "question_text_changed"
            return current
        page.wait_for_timeout(300)
    return extract_question(page)


def classify_start_url(url: str) -> str:
    """Return assessment kind for in-progress URLs, else catalog/unknown."""
    path = urlparse(url).path.rstrip("/")
    if re.search(r"practice-milestone-\d+-\d+", path, re.I):
        return "practice_milestone"
    if re.search(r"(?<!practice-)milestone-\d+-\d+", path, re.I):
        return "milestone"
    if CHALLENGE_ID_RE.search(path):
        return "challenge"
    if "/spcc/" in path:
        return "catalog"
    return "unknown"


def challenge_id_from_url(url: str) -> str | None:
    match = ASSESSMENT_ID_RE.search(url or "")
    return match.group(1).lower() if match else None


def unit_catalog_url_from_any(url: str) -> str | None:
    """Derive unit catalog URL from a challenge/milestone/question URL when possible."""
    parsed = urlparse(url)
    path = parsed.path
    match = re.search(
        r"(/spcc/[^/]+?)-(?:challenge|practice-milestone|milestone)-\d+-\d+",
        path,
        re.I,
    )
    if match:
        return f"{parsed.scheme}://{parsed.netloc}{match.group(1)}"
    if classify_start_url(url) == "catalog":
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    return None


def confirm_enter_assessment_modal(
    page,
    *,
    title: str,
    kind: str,
    timeout_ms: int = 600_000,
) -> dict:
    """Show OUR confirmation overlay (not Sophia's). Wait for Allow / Cancel."""
    kind_label = {
        "practice_milestone": "Practice Milestone",
        "milestone": "Milestone",
        "challenge": "Challenge",
    }.get(kind, kind or "Assessment")
    print(
        f"CONSENT_MODAL_SHOW kind={kind} title={title!r} "
        f"(waiting for Allow/Cancel in browser)",
        flush=True,
    )
    page.evaluate(
        """({ modalId, title, kindLabel }) => {
          const old = document.getElementById(modalId);
          if (old) old.remove();
          const safeTitle = String(title || '').replace(/[<>&]/g, '');
          const root = document.createElement('div');
          root.id = modalId;
          root.setAttribute('data-result', '');
          root.style.cssText = [
            'position:fixed', 'inset:0', 'z-index:2147483647',
            'display:flex', 'align-items:center', 'justify-content:center',
            'background:rgba(10,16,28,0.62)', 'font-family:ui-sans-serif,system-ui,sans-serif'
          ].join(';');
          root.innerHTML =
            '<div style="width:min(520px,92vw);background:#111827;color:#F9FAFB;border:1px solid #374151;' +
            'border-radius:14px;box-shadow:0 24px 80px rgba(0,0,0,.45);padding:22px 22px 18px;">' +
            '<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#93C5FD;margin-bottom:8px;">' +
            'Agent confirmation</div>' +
            '<div style="font-size:20px;font-weight:700;line-height:1.3;margin-bottom:10px;">' +
            'Enter ' + kindLabel + '?</div>' +
            '<div style="font-size:14px;line-height:1.55;color:#D1D5DB;margin-bottom:18px;">' +
            'Next catalog item is <b style="color:#FFF">' + safeTitle + '</b>. ' +
            'This is not a Challenge page. Click <b>Allow</b> to let the agent open it and continue answering.' +
            '</div>' +
            '<div style="display:flex;gap:10px;justify-content:flex-end;">' +
            '<button id="' + modalId + '-cancel" type="button" style="padding:10px 16px;border-radius:10px;' +
            'border:1px solid #4B5563;background:#1F2937;color:#E5E7EB;cursor:pointer;">Cancel</button>' +
            '<button id="' + modalId + '-allow" type="button" style="padding:10px 16px;border-radius:10px;' +
            'border:0;background:#2563EB;color:white;font-weight:600;cursor:pointer;">Allow</button>' +
            '</div></div>';
          document.documentElement.appendChild(root);
          const finish = (value) => { root.setAttribute('data-result', value); };
          root.querySelector('#' + modalId + '-allow').addEventListener('click', () => finish('allow'));
          root.querySelector('#' + modalId + '-cancel').addEventListener('click', () => finish('cancel'));
        }""",
        {"modalId": CONSENT_MODAL_ID, "title": title, "kindLabel": kind_label},
    )
    try:
        page.wait_for_function(
            """(modalId) => {
              const el = document.getElementById(modalId);
              if (!el) return false;
              const v = el.getAttribute('data-result') || '';
              return v === 'allow' || v === 'cancel';
            }""",
            arg=CONSENT_MODAL_ID,
            timeout=timeout_ms,
        )
        decision = page.evaluate(
            """(modalId) => {
              const el = document.getElementById(modalId);
              const value = el ? (el.getAttribute('data-result') || '') : '';
              if (el) el.remove();
              return value;
            }""",
            CONSENT_MODAL_ID,
        )
    except Exception as exc:
        page.evaluate(
            """(modalId) => { const el = document.getElementById(modalId); if (el) el.remove(); }""",
            CONSENT_MODAL_ID,
        )
        print(f"CONSENT_MODAL_TIMEOUT {type(exc).__name__}:{exc}", flush=True)
        return {"ok": False, "decision": "timeout", "error": f"{type(exc).__name__}:{exc}"}

    allowed = decision == "allow"
    print(f"CONSENT_MODAL_DECISION decision={decision}", flush=True)
    return {"ok": allowed, "decision": decision}


def accept_site_milestone_ready_modal(page) -> dict:
    """If Sophia shows its own Are You Ready? modal after click, press Continue."""
    result = {"attempted": False}
    try:
        # Prefer the ready modal Continue button when present.
        continue_btn = page.locator(
            ".sophypop-container.sophypop-is-open button, .flexible-popup button, [role='dialog'] button"
        ).filter(has_text=re.compile(r"^(Continue|Start|Begin)$", re.I))
        if continue_btn.count() > 0 and continue_btn.first.is_visible():
            result["attempted"] = True
            continue_btn.first.click(timeout=5000)
            page.wait_for_timeout(1500)
            result["ok"] = True
            result["click"] = "continue"
            return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}:{exc}"
    return result


def probe_exit_and_catalog(page) -> dict:
    return page.evaluate(
        """() => {
          const exitLinks = Array.from(document.querySelectorAll('a'))
            .filter(a => /exit the challenge/i.test((a.textContent || '').trim()))
            .map(a => ({href: a.getAttribute('href') || '', text: (a.textContent || '').trim(), className: a.className}));
          const titles = Array.from(document.querySelectorAll('.assessment-item-link-title'))
            .map(el => {
              const link = el.closest('a') || el.parentElement?.closest?.('a');
              return {
                title: (el.textContent || '').trim(),
                href: link ? (link.getAttribute('href') || '') : '',
              };
            });
          return {url: location.href, exitLinks, titles: titles.slice(0, 20)};
        }"""
    )


def dismiss_blocking_overlays(page) -> dict:
    """Close Sophia well-done / cookie overlays that intercept Exit clicks."""
    try:
        closed = page.evaluate(
            """() => {
              const closed = [];
              const roots = Array.from(document.querySelectorAll(
                '.sophypop-container.sophypop-is-open, [role="dialog"].sophypop-is-open, .well-done-message-overlay'
              ));
              for (const root of roots) {
                const btn = root.querySelector(
                  'button, a, [role="button"], .close, .sophypop-close, [aria-label*="close" i], [aria-label*="Close" i]'
                );
                if (btn) {
                  btn.click();
                  closed.push('button');
                  continue;
                }
                root.classList.remove('sophypop-is-open');
                root.style.display = 'none';
                root.style.pointerEvents = 'none';
                closed.push('force_hide');
              }
              return closed;
            }"""
        )
        if closed:
            page.wait_for_timeout(400)
        return {"closed": closed}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}:{exc}"}


def click_exit_challenge(page) -> dict:
    """Click Exit the Challenge and land on the unit catalog when possible."""
    result: dict = {"attempted": False, "before": probe_exit_and_catalog(page)}
    result["overlay"] = dismiss_blocking_overlays(page)
    # 等待 "Exit the Challenge" 按钮出现（challenge 完成后弹窗可能需要几秒加载）
    try:
        page.locator("a, button", has_text="Exit the Challenge").first.wait_for(
            state="visible", timeout=10000
        )
    except Exception:
        pass  # 超时也继续尝试
    candidates = [
        page.locator("a.w-button", has_text="Exit the Challenge"),
        page.locator("a", has_text="Exit the Challenge"),
        page.locator("button", has_text="Exit the Challenge"),
    ]
    for locator in candidates:
        try:
            if locator.count() == 0:
                continue
            target = locator.first
            if not target.is_visible():
                continue
            result["attempted"] = True
            result["text"] = target.inner_text().strip()
            href = target.get_attribute("href")
            result["href"] = href
            try:
                target.click(timeout=8000)
            except Exception:
                # Overlay or animation may still intercept; force navigation via href.
                dismiss_blocking_overlays(page)
                if href:
                    abs_href = href if href.startswith("http") else f"https://app.sophia.org{href}"
                    page.goto(abs_href, wait_until="domcontentloaded")
                else:
                    target.click(timeout=5000, force=True)
            page.wait_for_timeout(2000)
            result["after_url"] = page.url
            result["after"] = probe_exit_and_catalog(page)
            result["ok"] = classify_start_url(page.url) == "catalog" or bool(
                page.locator(".assessment-item-link-title").count()
            )
            return result
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}:{exc}"
            continue
    result["reason"] = "exit_not_found"
    return result


def list_catalog_challenges(page) -> list[dict]:
    """Probe catalog rows for Challenge / Practice Milestone / Milestone.

    Runtime-verified signals:
    - challenge: .assessment-item.challenge
    - practice: .assessment-item.practice-milestone
    - milestone: .assessment-item.milestone
    - completed: visited + full score N/N when present
    - locked/unavailable: show-trial-explanation or empty non-anchor
    """
    return page.evaluate(
        """() => {
          const items = Array.from(document.querySelectorAll(
            '.assessment-item.challenge, .assessment-item.practice-milestone, .assessment-item.milestone'
          ));
          return items.map((item, index) => {
            const titleEl = item.querySelector('.assessment-item-link-title');
            const title = titleEl ? titleEl.textContent.trim().replace(/\\s+/g, ' ') : '';
            const wrapper = item.closest('a.assessment-item-wrapper, .assessment-item-wrapper');
            const link = item.closest('a.assessment-item-wrapper') || (wrapper && wrapper.tagName === 'A' ? wrapper : null);
            const wrapperClass = wrapper ? String(wrapper.className || '') : '';
            const itemClass = String(item.className || '');
            const href = link ? (link.getAttribute('href') || link.getAttribute('data-base-href') || '') : '';
            const showTrialExplanation = /\\bshow-trial-explanation\\b/.test(wrapperClass);
            const visitedClass = /\\bvisited\\b/.test(wrapperClass);
            const isAnchor = !!(link && link.tagName === 'A');
            const openModal = !!(wrapper && wrapper.hasAttribute('data-open-modal'));
            const scoreEl = item.querySelector('.score-block');
            const scoreText = scoreEl ? (scoreEl.textContent || '').trim().replace(/\\s+/g, '') : '';
            const scoreMatch = scoreText.match(/^(\\d+)\\/(\\d+)$/);
            let attempted = null;
            let total = null;
            if (scoreMatch) {
              attempted = parseInt(scoreMatch[1], 10);
              total = parseInt(scoreMatch[2], 10);
            }
            const svg = item.querySelector('svg.svg-attempted-indicator[data-attempted][data-total-questions]');
            if (svg) {
              const a = parseInt(svg.getAttribute('data-attempted') || '', 10);
              const t = parseInt(svg.getAttribute('data-total-questions') || '', 10);
              if (!Number.isNaN(a)) attempted = a;
              if (!Number.isNaN(t)) total = t;
            }
            const hasOkScoreClass = !!(scoreEl && /\\bok-score\\b/.test(String(scoreEl.className || '')));
            const completed = (
              (attempted != null && total != null && total > 0 && attempted >= total)
              || (hasOkScoreClass && attempted != null && total != null && attempted >= total)
            );
            const locked = showTrialExplanation || (!isAnchor && !href && !openModal);
            let status = 'unknown';
            if (locked) status = 'locked';
            else if (completed) status = 'completed';
            else if ((isAnchor && href) || openModal) status = (attempted != null && attempted > 0) ? 'in_progress' : 'available';
            else status = 'unavailable';

            let kind = 'challenge';
            if (/\\bpractice-milestone\\b/.test(itemClass) || /practice\\s*milestone/i.test(title)) kind = 'practice_milestone';
            else if (/\\bmilestone\\b/.test(itemClass) || /^milestone\\b/i.test(title)) kind = 'milestone';

            let challengeId = '';
            const hrefMatch = (href || '').match(/(?:practice-)?milestone-\\d+-\\d+|challenge-\\d+-\\d+/i);
            const titleChallenge = title.match(/challenge\\s+(\\d+)\\.(\\d+)/i);
            const titlePractice = title.match(/practice\\s*milestone\\s*(\\d+)/i);
            const titleMilestone = title.match(/^milestone\\s*(\\d+)/i);
            const km = link ? (link.getAttribute('data-km-value') || '') : '';
            const kmMatch = km.match(/((?:practice-)?milestone|challenge)-(\\d+)-(\\d+)/i);
            if (hrefMatch) challengeId = hrefMatch[0].toLowerCase();
            else if (kmMatch) challengeId = `${kmMatch[1].toLowerCase()}-${kmMatch[2]}-${kmMatch[3]}`;
            else if (titleChallenge) challengeId = `challenge-${titleChallenge[1]}-${titleChallenge[2]}`;
            else if (titlePractice) challengeId = `practice-milestone-${titlePractice[1]}-1`;
            else if (titleMilestone) challengeId = `milestone-${titleMilestone[1]}-1`;

            return {
              index,
              title,
              href,
              challengeId,
              kind,
              status,
              completed,
              locked,
              openModal,
              available: status === 'available' || status === 'in_progress',
              isAnchor,
              visitedClass,
              showTrialExplanation,
              attempted,
              total,
              scoreText,
              hasOkScoreClass,
              wrapperTag: wrapper ? wrapper.tagName : '',
              wrapperClass,
              itemClass,
            };
          }).filter(item => item.title || item.challengeId || item.href);
        }"""
    )


def _load_done_practice_milestones() -> set[str]:
    """Load completed practice milestone IDs from persistent file."""
    try:
        data = json.loads(DONE_PM_PATH.read_text(encoding="utf-8"))
        return {str(x).strip().lower() for x in data}
    except Exception:
        return set()


def _save_done_practice_milestone(cid: str) -> None:
    """Record a completed practice milestone ID to persistent file."""
    cid = (cid or "").strip().lower()
    if not cid:
        return
    done = _load_done_practice_milestones()
    done.add(cid)
    try:
        DONE_PM_PATH.write_text(json.dumps(sorted(done)), encoding="utf-8")
    except Exception:
        pass


def _load_done_milestones() -> set[str]:
    """Load completed milestone IDs from persistent file."""
    try:
        data = json.loads(DONE_MILESTONE_PATH.read_text(encoding="utf-8"))
        return {str(x).strip().lower() for x in data}
    except Exception:
        return set()


def _save_done_milestone(cid: str) -> None:
    """Record a completed milestone ID to persistent file."""
    cid = (cid or "").strip().lower()
    if not cid:
        return
    done = _load_done_milestones()
    done.add(cid)
    try:
        DONE_MILESTONE_PATH.write_text(json.dumps(sorted(done)), encoding="utf-8")
    except Exception:
        pass


def enter_next_challenge(page, *, visited: set[str], preferred_catalog_url: str | None = None, skip_ids: str = "") -> dict:
    """From unit catalog, click the next unvisited / incomplete challenge link."""
    if preferred_catalog_url and classify_start_url(page.url) != "catalog":
        page.goto(preferred_catalog_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

    # Wait for catalog list
    try:
        page.locator(".assessment-item-link-title").first.wait_for(state="visible", timeout=15000)
    except Exception:
        page.wait_for_timeout(2000)

    challenges = list_catalog_challenges(page)
    result: dict = {
        "url": page.url,
        "challenges": challenges,
        "visited": sorted(visited),
        "clicked": None,
    }
    # #region agent log
    _agent_log(
        "G",
        "login_then_select.py:enter_next_challenge:catalog",
        "Catalog challenge DOM states before click",
        {
            "url": page.url,
            "count": len(challenges),
            "states": [
                {
                    "title": c.get("title"),
                    "challengeId": c.get("challengeId"),
                    "kind": c.get("kind"),
                    "status": c.get("status"),
                    "scoreText": c.get("scoreText"),
                    "locked": c.get("locked"),
                    "completed": c.get("completed"),
                    "isAnchor": c.get("isAnchor"),
                    "openModal": c.get("openModal"),
                }
                for c in challenges[:20]
            ],
        },
    )
    # #endregion

    # Pick first actionable challenge in DOM order using real status:
    # skip session-visited, locked/unavailable, --skip rows,
    # ALL milestones (practice + regular) — milestones are only automated
    # when the user manually enters them and starts/resumes.
    skip_set = {s.strip().lower() for s in skip_ids.split(",") if s.strip()}
    done_pm = _load_done_practice_milestones()
    done_milestones = _load_done_milestones()
    pick = None
    for item in challenges:
        cid = (item.get("challengeId") or "").lower()
        if cid and cid in visited:
            continue
        if cid and cid in skip_set:
            continue
        # 从目录运行时跳过所有 practice milestone 和 milestone
        # milestone 只在用户手动进入页面后才自动化
        if item.get("kind") in {"practice_milestone", "milestone"}:
            continue
        if item.get("completed") or item.get("status") == "completed":
            continue
        if item.get("locked") or item.get("status") in {"locked", "unavailable"}:
            continue
        if item.get("available") or item.get("status") in {"available", "in_progress"}:
            pick = item
            break

    if pick is None:
        # Fallback: first non-completed, non-visited challenge (skip milestones).
        for item in challenges:
            cid = (item.get("challengeId") or "").lower()
            if cid and cid in visited:
                continue
            if item.get("completed"):
                continue
            if item.get("kind") in {"practice_milestone", "milestone"}:
                continue
            if item.get("isAnchor") and item.get("href"):
                pick = item
                break

    if pick is None:
        locked_only = all(c.get("locked") or c.get("completed") for c in challenges) if challenges else False
        result["reason"] = "all_remaining_locked" if locked_only else "no_next_challenge"
        print(
            f"NO_NEXT_CHALLENGE reason={result['reason']} "
            f"states={[ (c.get('challengeId'), c.get('status'), c.get('scoreText')) for c in challenges[:8] ]}",
            flush=True,
        )
        return result

    title = pick.get("title") or ""
    href = pick.get("href") or ""
    kind = (pick.get("kind") or "challenge").lower()
    compact_title = re.sub(r"\s+", " ", title).strip()
    url_before = page.url

    # For Practice Milestone / Milestone: OUR consent popup first (not Sophia's).
    if kind in {"practice_milestone", "milestone"}:
        consent = confirm_enter_assessment_modal(page, title=compact_title or href, kind=kind)
        result["consent"] = consent
        if not consent.get("ok"):
            cid = (pick.get("challengeId") or "").lower()
            print(
                f"CONSENT_CANCELLED kind={kind} title={compact_title!r} — 暂停脚本，等待恢复",
                flush=True,
            )
            # Cancel = 暂停：创建 pause.flag，阻塞等待恢复
            PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            PAUSE_FLAG.write_text("1", "utf-8")
            check_pause(page)
            # 恢复后：检测当前页面状态
            current_url = page.url
            current_kind = classify_start_url(current_url)
            print(
                f"RESUMED_AT url={current_url} kind={current_kind}",
                flush=True,
            )
            if current_kind in {"challenge", "practice_milestone", "milestone"}:
                # 用户手动进入了某个 challenge/milestone，直接在这里答题
                result["ok"] = True
                result["reason"] = "resumed_in_assessment"
                result["clicked"] = {"href": current_url, "kind": current_kind, "title": ""}
                return result
            # 还在目录或其他页面：标记跳过，继续找下一个
            if cid:
                visited.add(cid)
            # 如果不在目录，导航回目录
            if current_kind != "catalog":
                catalog = unit_catalog_url_from_any(current_url) or preferred_catalog_url or ""
                if catalog:
                    page.goto(catalog, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
            result["reason"] = "consent_cancelled_skipped"
            result["ok"] = False
            result["skipped"] = True
            return result

    # Enter by clicking the catalog title — use exact text match to avoid
    # "MILESTONE 1" matching "PRACTICE MILESTONE 1" (has_text is substring).
    try:
        clicked_js = page.evaluate(
            """(title) => {
              const titles = Array.from(document.querySelectorAll('.assessment-item-link-title'));
              for (const t of titles) {
                if (t.textContent.trim() === title) {
                  const link = t.closest('a') || t.parentElement?.closest?.('a');
                  if (link) { link.scrollIntoView({block:'center'}); link.click(); return 'link'; }
                  t.scrollIntoView({block:'center'}); t.click();
                  return 'span';
                }
              }
              return null;
            }""",
            compact_title,
        )
        if clicked_js:
            result["click_method"] = f"exact_{clicked_js}"
        else:
            result["reason"] = "challenge_title_not_found"
            return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}:{exc}"
        result["reason"] = "title_click_failed"
        return result

    page.wait_for_timeout(1500)

    # Fallback: 如果点击后跳到了 milestone_take_feedbacks 页面（上次的结果页），
    # 直接构造正确的 milestone URL 导航到第一题
    if "milestone_take_feedbacks" in page.url:
        unit_slug = (preferred_catalog_url or "").rstrip("/").split("/")[-1]
        pm_cid = (pick.get("challengeId") or "").lower()
        if unit_slug and pm_cid:
            direct_url = f"https://app.sophia.org/spcc/{unit_slug}-{pm_cid}/1"
            page.goto(direct_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

    # Sophia may show its own Are You Ready? modal for real Milestone.
    site_ready = accept_site_milestone_ready_modal(page)
    if site_ready.get("attempted"):
        result["site_ready_modal"] = site_ready
        page.wait_for_timeout(1000)

    # Some catalog links open overview; wait for a question if present.
    try:
        page.locator(
            ".question-slide .challenge-v2-question__text, .challenge-v2-answer__input, "
            "label.milestone-answer__input, .assessment-question-block"
        ).first.wait_for(state="visible", timeout=15000)
    except Exception:
        for label in ("Start Challenge", "Resume", "Continue", "Begin", "Start"):
            btn = page.locator("button, a", has_text=label)
            try:
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=5000)
                    page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

    cid = challenge_id_from_url(page.url) or (pick.get("challengeId") or "").lower()
    url_kind = classify_start_url(page.url)
    navigated = page.url != url_before or url_kind in {"challenge", "practice_milestone", "milestone"}
    has_question = extract_question(page) is not None
    if cid and (navigated or has_question):
        visited.add(cid)
    result["clicked"] = {
        "title": title,
        "href": href,
        "challengeId": cid,
        "kind": kind,
        "url_before": url_before,
        "url_after": page.url,
        "navigated": navigated,
    }
    result["ok"] = has_question or url_kind in {"challenge", "practice_milestone", "milestone"}
    if not result["ok"]:
        result["reason"] = "title_clicked_but_still_on_catalog"
        print(
            f"ENTER_CHALLENGE_BLOCKED title={title!r} url={page.url} "
            f"(title click ok; page did not enter assessment — check account access)",
            flush=True,
        )
    else:
        print(f"ENTERED_CHALLENGE title={title!r} kind={kind} url={page.url}", flush=True)
    # #region agent log
    _agent_log(
        "G",
        "login_then_select.py:enter_next_challenge:after_title_click",
        "Catalog title click result (no URL synthesis)",
        {
            "title": title,
            "kind": kind,
            "click_method": result.get("click_method"),
            "ok": result["ok"],
            "reason": result.get("reason"),
            "url_before": url_before,
            "url_after": page.url,
            "consent": result.get("consent"),
        },
    )
    # #endregion
    return result


def advance_to_next_challenge_group(page, *, visited: set[str], catalog_url: str | None, skip_ids: str = "") -> dict:
    """No more questions in current challenge: Exit -> catalog -> next challenge."""
    # #region agent log
    _agent_log(
        "G",
        "login_then_select.py:advance_to_next_challenge_group:start",
        "Attempting inter-challenge transition",
        {"url": page.url, "visited": sorted(visited), "catalog_url": catalog_url},
    )
    # #endregion
    # Mark current challenge visited
    current_id = challenge_id_from_url(page.url)
    if current_id:
        visited.add(current_id)

    # Try continue once more in case a late CTA appears
    click_continue_if_present(page)
    page.wait_for_timeout(500)

    exit_result = click_exit_challenge(page)
    # #region agent log
    _agent_log(
        "G",
        "login_then_select.py:advance_to_next_challenge_group:exit",
        "Exit the Challenge click result",
        exit_result,
    )
    # #endregion
    print(f"EXIT_CHALLENGE attempted={exit_result.get('attempted')} ok={exit_result.get('ok')} url={page.url}", flush=True)

    if not exit_result.get("ok"):
        # Fallback: navigate to derived catalog URL
        fallback = catalog_url or unit_catalog_url_from_any(page.url) or exit_result.get("href")
        if fallback:
            href = fallback
            if isinstance(href, str) and href.startswith("/"):
                href = f"https://app.sophia.org{href}"
            page.goto(str(href), wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

    # Cancel 跳过后重试，直到成功进入或无更多 challenge
    while True:
        enter_result = enter_next_challenge(
            page,
            visited=visited,
            preferred_catalog_url=catalog_url or unit_catalog_url_from_any(page.url),
            skip_ids=skip_ids,
        )
        if enter_result.get("ok"):
            break
        if enter_result.get("skipped"):
            continue  # Cancel 跳过，找下一个
        break  # 其他失败（无更多 challenge）
    # #region agent log
    _agent_log(
        "G",
        "login_then_select.py:advance_to_next_challenge_group:enter",
        "Enter next challenge result",
        enter_result,
    )
    # #endregion
    return {
        "exit": exit_result,
        "enter": enter_result,
        "ok": bool(enter_result.get("ok")),
        "clicked": enter_result.get("clicked"),
    }


def publish_question(extracted: dict, round_idx: int) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if ANSWER_PATH.exists():
        ANSWER_PATH.unlink()
    payload = {
        "round": round_idx,
        "url": extracted.get("url"),
        "question": extracted.get("question"),
        "options": extracted.get("options"),
        "written_at": int(time.time() * 1000),
        "instruction": (
            "Write logs/pending_answer.json then save, e.g. "
            '{"option_index": 2, "rationale": "reasoning..."} or {"option_text": "fixed content", "rationale": "reasoning..."}'
        ),
    }
    QUESTION_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"QUESTION_PUBLISHED {QUESTION_PATH}", flush=True)


def resolve_option_from_answer(answer: dict, options: list[str]) -> tuple[str, int]:
    if "option_index" in answer:
        index = int(answer["option_index"])
        if 0 <= index < len(options):
            return options[index], index
    if "option_text" in answer and str(answer["option_text"]).strip():
        text = str(answer["option_text"]).strip()
        needle = _normalize_option_text(text)
        for index, option in enumerate(options):
            normalized = _normalize_option_text(option)
            if needle == normalized or needle in normalized or normalized in needle:
                return option, index
        return text, -1
    raise ValueError(f"invalid answer payload: {answer}")


def wait_for_ide_answer(page, options: list[str], timeout_s: int = ANSWER_WAIT_MAX) -> dict:
    """Poll pending_answer.json written by local IDE / agent."""
    deadline = time.time() + timeout_s
    print(f"WAITING_IDE_ANSWER path={ANSWER_PATH}", flush=True)
    while time.time() < deadline:
        if ANSWER_PATH.exists():
            try:
                raw = ANSWER_PATH.read_text(encoding="utf-8").strip()
                if not raw:
                    page.wait_for_timeout(500)
                    continue
                answer = json.loads(raw)
                option_text, option_index = resolve_option_from_answer(answer, options)
                meta = {
                    "source": "ide_file",
                    "option_text": option_text,
                    "option_index": option_index,
                    "rationale": str(answer.get("rationale", "")),
                    "raw": answer,
                }
                try:
                    ANSWER_PATH.unlink()
                except Exception:
                    pass
                return meta
            except Exception as exc:
                print(f"ANSWER_PARSE_WAIT {type(exc).__name__}:{exc}", flush=True)
        page.wait_for_timeout(500)
    raise TimeoutError(f"timed out waiting for {ANSWER_PATH}")


def choose_answer(extracted: dict, round_idx: int, mode: str, seed_option: str) -> dict:
    if round_idx == 1 and seed_option:
        return {"source": "seed_argv", "option_text": seed_option, "rationale": "cli seed"}

    if mode == "ide":
        # page is passed by caller via wait; this helper only for llm path without page
        raise RuntimeError("ide mode must use wait_for_ide_answer")

    answer = answer_question(
        AnswerRequest(
            question=extracted["question"],
            options=extracted["options"],
            context="",
            images=extracted.get("images", []),
        )
    )
    meta = answer.dict() if hasattr(answer, "dict") else answer.model_dump()
    return meta


def run_challenge_loop(
    page,
    *,
    mode: str,
    seed_option: str,
    max_rounds: int,
    global_round_start: int = 1,
    context=None,
    storage_state_path: Path | None = None,
    challenge_kind: str = "challenge",
    extract_only: bool = False,
    manual_answers: list[int] | None = None,
) -> dict:
    """Solve questions inside the current challenge. Returns why it stopped."""
    timing = _TIMING.get(challenge_kind, _TIMING["milestone"])
    # 手动答案模式：不等 LLM，所有等待时间减半（2 倍速）
    if manual_answers is not None:
        timing = {k: max(v // 2, 300) for k, v in timing.items()}
    # #region agent log
    _agent_log(
        "A",
        "login_then_select.py:run_challenge_loop:start",
        "Entering intra-challenge loop",
        {
            "has_auto_submit_loop": True,
            "mode": mode,
            "seed_option": seed_option,
            "max_rounds": max_rounds,
            "url": page.url,
            "challenge_id": challenge_id_from_url(page.url),
        },
    )
    # #endregion

    status = {"stop_reason": "max_rounds", "rounds_done": 0, "last_question": ""}
    seed_used = False
    qa_records = []  # 收集所有题目的问答记录，用于生成报告

    for local_round in range(1, max_rounds + 1):
        round_idx = global_round_start + local_round - 1
        if check_pause(page) == "mode_switch":
            print("MODE_SWITCH breaking challenge loop", flush=True)
            status["stop_reason"] = "mode_switch"
            break
        # 进入题目后等待让页面内容完全渲染（challenge 快，milestone 慢）
        page.wait_for_timeout(timing["enter_wait_ms"])
        extracted = extract_question(page)
        exit_visible = page.locator("a, button", has_text="Exit the Challenge").count() > 0
        submit_visible = page.locator("button", has_text="Submit My Answer").count() > 0
        if not extracted or (exit_visible and not submit_visible):
            # #region agent log
            _agent_log(
                "A",
                "login_then_select.py:run_challenge_loop:no_question",
                "No actionable question; challenge may be finished",
                {
                    "round": round_idx,
                    "url": page.url,
                    "extracted": bool(extracted),
                    "exit_visible": exit_visible,
                    "submit_visible": submit_visible,
                    "probe": probe_exit_and_catalog(page),
                },
            )
            # #endregion
            print(
                f"NO_QUESTION round={round_idx} exit_visible={exit_visible} submit_visible={submit_visible}",
                flush=True,
            )
            status["stop_reason"] = "no_question"
            break

        print(
            f"EXTRACTED round={round_idx} question={extracted['question']!r} options={len(extracted['options'])} images={len(extracted.get('images', []))}",
            flush=True,
        )
        publish_question(extracted, round_idx)
        # #region agent log
        _agent_log(
            "A",
            "login_then_select.py:run_challenge_loop:extracted",
            "Extracted question for IDE/LLM answer",
            {
                "round": round_idx,
                "question": extracted["question"][:240],
                "options": extracted["options"],
                "url": extracted["url"],
                "mode": mode,
            },
        )
        # #endregion

        if extract_only:
            # 只提取题目模式：记录题目，通过导航箭头切换到下一题，不答题不提交
            qa_records.append({
                "round": round_idx,
                "url": extracted.get("url", ""),
                "question": extracted.get("question", ""),
                "options": extracted.get("options", []),
            })
            qa_path = LOGS_DIR / "extracted_questions.json"
            qa_path.write_text(json.dumps(qa_records, ensure_ascii=False, indent=2), "utf-8")
            print(f"EXTRACT_ONLY round={round_idx} recorded (total={len(qa_records)})", flush=True)
            status["rounds_done"] = local_round
            status["last_question"] = extracted["question"]

            # 通过导航箭头（fa-chevron-right）切换到下一题
            nav_ok = page.evaluate(
                """() => {
                    const arrow = document.querySelector('.right-arrow, a[title="Next Challenge Question"]');
                    if (arrow) { arrow.click(); return true; }
                    return false;
                }"""
            )
            if not nav_ok:
                print("EXTRACT_END no_right_arrow", flush=True)
                status["stop_reason"] = "no_new_question"
                break
            page.wait_for_timeout(timing["enter_wait_ms"])

            nxt = extract_question(page)
            if not nxt or nxt.get("question") == extracted["question"]:
                print("EXTRACT_END no_new_question_after_nav", flush=True)
                status["stop_reason"] = "no_new_question"
                break
            print(f"EXTRACT_NEXT round={round_idx + 1} q={nxt['question']!r}", flush=True)
            continue

        elif manual_answers is not None:
            # 手动答案序列模式：按用户给定的 abcd 序列依次选择，不调 LLM
            if not manual_answers:
                print("MANUAL_DONE all answers consumed", flush=True)
                status["stop_reason"] = "manual_done"
                break
            mi = int(manual_answers.pop(0))
            opts = extracted["options"]
            if 0 <= mi < len(opts):
                option_index = mi
                option_text = opts[mi]
            else:
                print(
                    f"MANUAL_WARN round={round_idx} index={mi} out of range (options={len(opts)}), fallback to 0",
                    flush=True,
                )
                option_index = 0
                option_text = opts[0]
            answer_meta = {
                "source": "manual",
                "option_text": option_text,
                "option_index": option_index,
                "rationale": "manual answer sequence",
            }

        elif (not seed_used) and seed_option:
            seed_used = True
            option_text = seed_option
            option_index = -1
            needle = _normalize_option_text(seed_option)
            for i, option in enumerate(extracted["options"]):
                if needle in _normalize_option_text(option):
                    option_index = i
                    option_text = option
                    break
            answer_meta = {"source": "seed_argv", "option_text": option_text, "option_index": option_index}
        elif mode == "ide":
            answer_meta = wait_for_ide_answer(page, extracted["options"])
            option_text = answer_meta["option_text"]
            option_index = int(answer_meta.get("option_index", -1))
        else:
            answer_meta = choose_answer(extracted, round_idx, mode, seed_option)
            option_text = answer_meta["option_text"]
            option_index = int(answer_meta.get("option_index", -1))

        # #region agent log
        _agent_log(
            "A",
            "login_then_select.py:run_challenge_loop:answer",
            "Answer chosen for select",
            {"round": round_idx, "option_text": option_text, "option_index": option_index, "answer": answer_meta},
        )
        # #endregion
        print(
            f"ANSWER round={round_idx} index={option_index} option={_normalize_option_text(option_text)!r} "
            f"source={answer_meta.get('source')}",
            flush=True,
        )

        # 记录本题问答，用于生成报告
        qa_records.append({
            "round": round_idx,
            "url": extracted.get("url", ""),
            "question": extracted.get("question", ""),
            "options": extracted.get("options", []),
            "answer_index": option_index,
            "answer_text": option_text,
            "answer_source": answer_meta.get("source", ""),
            "rationale": answer_meta.get("rationale", ""),
        })
        # 实时保存到 JSON
        qa_path = LOGS_DIR / "milestone_answers.json"
        qa_path.write_text(json.dumps(qa_records, ensure_ascii=False, indent=2), "utf-8")

        select_result = select_option(
            page,
            option_text,
            option_index=option_index if option_index >= 0 else None,
        )
        page.wait_for_timeout(timing["after_select_wait_ms"])
        # #region agent log
        _agent_log(
            "B",
            "login_then_select.py:run_challenge_loop:after_select",
            "DOM after option select",
            {
                "round": round_idx,
                "select_result": select_result,
                "submit": _submit_dom_probe(page),
                "selection": _selection_dom_probe(page),
            },
        )
        # #endregion
        print(
            f"OPTION_SELECTED round={round_idx} method={select_result.get('method')} "
            f"text_match={_normalize_option_text(option_text)!r}",
            flush=True,
        )

        submit_result = click_submit(page, submit_wait_ms=timing["submit_wait_ms"])
        # #region agent log
        _agent_log(
            "E",
            "login_then_select.py:run_challenge_loop:after_submit",
            "Submit click result",
            {"round": round_idx, "submit_result": submit_result},
        )
        # #endregion
        print(f"SUBMIT_CLICKED round={round_idx} result={submit_result.get('click')}", flush=True)
        status["rounds_done"] = local_round
        status["last_question"] = extracted["question"]
        if context is not None and storage_state_path is not None:
            persist_storage_state(context, storage_state_path, reason=f"after_submit_r{round_idx}")

        if str(submit_result.get("click", "")).startswith("fail") or not submit_result.get("attempted"):
            print("SUBMIT_FAILED stopping challenge loop", flush=True)
            status["stop_reason"] = "submit_failed"
            break

        # 手动答案序列用完：停在最后一题，不再继续
        if manual_answers is not None and not manual_answers:
            print("MANUAL_DONE all answers used, stopping at last question", flush=True)
            status["stop_reason"] = "manual_done"
            break

        nxt = wait_for_next_question(page, previous_question=extracted["question"], timeout_ms=timing["next_question_wait_ms"])
        # #region agent log
        _agent_log(
            "A",
            "login_then_select.py:run_challenge_loop:next_question",
            "Next question wait result",
            {
                "round": round_idx,
                "found": bool(nxt),
                "same_question": bool(nxt and nxt.get("question") == extracted["question"]),
                "next_question": (nxt or {}).get("question", "")[:240],
                "advanced_via": (nxt or {}).get("advanced_via"),
                "url": page.url,
            },
        )
        # #endregion
        if not nxt or nxt.get("question") == extracted["question"]:
            print("CHALLENGE_END no_new_question", flush=True)
            status["stop_reason"] = "no_new_question"
            break
        print(f"NEXT_QUESTION round={round_idx + 1} q={nxt['question']!r}", flush=True)

        # 进入新题目后，等待让页面内容完全渲染（challenge 快，milestone 慢）
        page.wait_for_timeout(timing["enter_wait_ms"])

    print(f"CHALLENGE_LOOP_DONE reason={status['stop_reason']}", flush=True)

    # extract-only 模式：如果一轮都没提取到，给出明确警告
    if extract_only and not qa_records:
        print("EXTRACT_ONLY_WARNING 未提取到任何题目！请确认浏览器当前页面包含可提取的题目（未完成的 challenge/milestone）", flush=True)

    # 生成报告
    if qa_records and extract_only:
        # extract-only 模式：生成题目提取报告（无答案）
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        report_name = f"extract_report_{ts_str}.md"
        report_path = LOGS_DIR / report_name
        lines = [
            f"# 题目提取报告",
            f"",
            f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"> 总题数: {len(qa_records)}",
            f"> 模式: 仅提取（未答题）",
            f"",
            f"---",
            f"",
        ]
        for rec in qa_records:
            url_num = rec["url"].split("/")[-1] if rec["url"] else "?"
            lines.append(f"## 第 {rec['round']} 题 (URL /{url_num})")
            lines.append(f"")
            lines.append(f"**{rec['question']}**")
            lines.append(f"")
            for i, opt in enumerate(rec["options"]):
                lines.append(f"- {chr(65+i)}) {opt}")
            lines.append(f"")
            lines.append(f"---")
            lines.append(f"")
        report_path.write_text("\n".join(lines), "utf-8")
        print(f"EXTRACT_REPORT_GENERATED name={report_name} path={report_path}", flush=True)
    elif qa_records and challenge_kind == "milestone":
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        report_name = f"milestone_report_{ts_str}.md"
        report_path = LOGS_DIR / report_name
        lines = [
            f"# Milestone 答题报告",
            f"",
            f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"> 总题数: {len(qa_records)}",
            f"",
            f"---",
            f"",
        ]
        for rec in qa_records:
            url_num = rec["url"].split("/")[-1] if rec["url"] else "?"
            lines.append(f"## 第 {rec['round']} 题 (URL /{url_num})")
            lines.append(f"")
            lines.append(f"**{rec['question']}**")
            lines.append(f"")
            for i, opt in enumerate(rec["options"]):
                marker = " ✅" if i == rec["answer_index"] else ""
                lines.append(f"- {chr(65+i)}) {opt}{marker}")
            lines.append(f"")
            lines.append(f"> 答案: **{chr(65+rec['answer_index'])}** — {rec['answer_text']}")
            lines.append(f"")
            if rec.get("rationale"):
                lines.append(f"> 推理: {rec['rationale']}")
                lines.append(f"")
            lines.append(f"---")
            lines.append(f"")
        report_path.write_text("\n".join(lines), "utf-8")
        print(f"REPORT_GENERATED name={report_name} path={report_path}", flush=True)
    elif qa_records:
        # 普通 challenge 只保存 JSON，不生成 MD 报告
        qa_path = LOGS_DIR / "challenge_answers.json"
        qa_path.write_text(json.dumps(qa_records, ensure_ascii=False, indent=2), "utf-8")
        print(f"CHALLENGE_ANSWERS_SAVED path={qa_path} count={len(qa_records)}", flush=True)

    return status


def run_unit_loop(
    page,
    *,
    start_url: str,
    mode: str,
    seed_option: str,
    max_rounds: int,
    max_challenges: int,
    context=None,
    storage_state_path: Path | None = None,
    skip_ids: str = "",
    extract_only: bool = False,
    manual_answers: list[int] | None = None,
) -> str:
    """Outer loop: catalog/challenge entry + intra-challenge + Exit + next challenge.
    返回停止原因（供主循环判断后续行为）。"""
    url_kind = classify_start_url(start_url)
    catalog_url = unit_catalog_url_from_any(start_url)
    visited: set[str] = set()
    # #region agent log
    _agent_log(
        "G",
        "login_then_select.py:run_unit_loop:start",
        "Unit/group loop start",
        {
            "start_url": start_url,
            "url_kind": url_kind,
            "catalog_url": catalog_url,
            "max_challenges": max_challenges,
            "max_rounds": max_rounds,
        },
    )
    # #endregion
    print(f"START_KIND={url_kind} catalog={catalog_url}", flush=True)

    if url_kind == "catalog":
        # Cancel 跳过后重试，直到成功进入或无更多 challenge
        while True:
            enter = enter_next_challenge(page, visited=visited, preferred_catalog_url=start_url, skip_ids=skip_ids)
            if enter.get("ok"):
                break
            if enter.get("skipped"):
                continue  # Cancel 跳过，找下一个
            print("UNIT_STOP cannot_enter_first_challenge", flush=True)
            return "cannot_enter_first_challenge"
        catalog_url = catalog_url or start_url.rstrip("/")
        current_kind = (enter.get("clicked") or {}).get("kind", "")
    elif url_kind in {"challenge", "practice_milestone", "milestone"}:
        cid = challenge_id_from_url(page.url)
        if cid:
            pass
        current_kind = url_kind
    else:
        print(f"UNIT_STOP unknown_start_url {start_url}", flush=True)
        return "unknown_start_url"

    global_round = 1
    reason = "loop_exhausted"
    for challenge_idx in range(1, max_challenges + 1):
        if check_pause(page) == "mode_switch":
            print("MODE_SWITCH breaking unit loop", flush=True)
            reason = "mode_switch"
            break
        cid = challenge_id_from_url(page.url)
        print(f"CHALLENGE_START idx={challenge_idx} id={cid} url={page.url}", flush=True)
        # #region agent log
        _agent_log(
            "G",
            "login_then_select.py:run_unit_loop:challenge_start",
            "Starting challenge group",
            {"challenge_idx": challenge_idx, "challenge_id": cid, "url": page.url, "visited": sorted(visited)},
        )
        # #endregion

        status = run_challenge_loop(
            page,
            mode=mode,
            seed_option=seed_option if challenge_idx == 1 else "",
            max_rounds=max_rounds,
            global_round_start=global_round,
            context=context,
            storage_state_path=storage_state_path,
            challenge_kind=current_kind,
            extract_only=extract_only,
            manual_answers=manual_answers,
        )
        global_round += int(status.get("rounds_done") or 0)
        if context is not None and storage_state_path is not None:
            persist_storage_state(context, storage_state_path, reason=f"after_challenge_{cid or challenge_idx}")

        # practice milestone 做完后持久化记录，下次自动跳过
        if current_kind == "practice_milestone" and status.get("stop_reason") == "no_new_question" and cid:
            _save_done_practice_milestone(cid)
            print(f"PRACTICE_MILESTONE_DONE id={cid} (saved to done_practice_milestones.json)", flush=True)

        # milestone 做完后持久化记录，下次自动跳过
        if current_kind == "milestone" and status.get("stop_reason") == "no_new_question" and cid:
            _save_done_milestone(cid)
            print(f"MILESTONE_DONE id={cid} (saved to done_milestones.json)", flush=True)

        if status.get("stop_reason") == "submit_failed":
            print("UNIT_STOP submit_failed", flush=True)
            reason = "submit_failed"
            break

        # 手动答案序列用完：不再进入下一个 challenge
        if status.get("stop_reason") == "manual_done":
            print("UNIT_DONE manual answers exhausted", flush=True)
            reason = "manual_done"
            break

        # 提取模式只组内循环：提完当前 challenge 即停，不进入下一个
        if extract_only and status.get("stop_reason") in {"no_new_question", "no_question"}:
            print("UNIT_DONE extract_only stopping at current challenge", flush=True)
            reason = "extract_done"
            break

        if challenge_idx >= max_challenges:
            print("UNIT_DONE max_challenges_reached", flush=True)
            reason = "max_challenges"
            break

        if status.get("stop_reason") in {"no_new_question", "no_question", "max_rounds"}:
            advanced = advance_to_next_challenge_group(
                page,
                visited=visited,
                catalog_url=catalog_url,
                skip_ids=skip_ids,
            )
            if not advanced.get("ok"):
                print("UNIT_STOP no_next_challenge_group", flush=True)
                # #region agent log
                _agent_log(
                    "G",
                    "login_then_select.py:run_unit_loop:no_next",
                    "Failed to advance to next challenge group",
                    advanced,
                )
                # #endregion
                reason = "no_next_challenge_group"
                break
            print(f"GROUP_ADVANCED to={page.url}", flush=True)
            current_kind = (advanced.get("clicked") or {}).get("kind", "")
            continue

        print(f"UNIT_STOP unexpected_reason={status.get('stop_reason')}", flush=True)
        reason = str(status.get("stop_reason") or "unexpected")
        break

    print("UNIT_LOOP_DONE", flush=True)
    return reason


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sophia select/submit loop with IDE or LLM answers")
    parser.add_argument(
        "url",
        help="Question URL (...-challenge-1-1/...) or unit catalog URL (.../introduction-to-web-development-3)",
    )
    parser.add_argument("seed_option", nargs="?", default="", help="Optional first-round option substring")
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=999,
        help="Max question rounds per challenge (default: 999 = 做完为止)",
    )
    parser.add_argument(
        "--max-challenges",
        type=int,
        default=999,
        help="Max challenge groups to traverse (default: 999 = 一直跑)",
    )
    parser.add_argument(
        "--mode",
        choices=["ide", "llm"],
        default="ide",
        help="ide=wait for logs/pending_answer.json; llm=LangGraph solver",
    )
    parser.add_argument(
        "--keep-open",
        dest="keep_open",
        action="store_true",
        default=True,
        help="流程结束后保持浏览器打开，等待 Ctrl+C 再关闭（默认开启）",
    )
    parser.add_argument(
        "--no-keep-open",
        dest="keep_open",
        action="store_false",
        help="流程结束后自动关闭浏览器",
    )
    parser.add_argument(
        "--skip",
        default="",
        help="逗号分隔的 challengeId 列表，跳过这些 challenge（如 practice-milestone-1-1）",
    )
    parser.add_argument(
        "--browser-only",
        dest="browser_only",
        action="store_true",
        default=False,
        help="只打开浏览器并登录，不启动自动化循环（用户手动导航后再启动自动化）",
    )
    parser.add_argument(
        "--extract-only",
        dest="extract_only",
        action="store_true",
        default=False,
        help="只提取题目，不答题不提交，遍历完后生成题目报告",
    )
    return parser.parse_args()


def main() -> None:
    # Load OPENAI_* from repo .env for --mode llm (no-op if already set).
    from solver.graph import _load_dotenv

    _load_dotenv()

    args = parse_args()
    target_url = args.url
    seed_option = args.seed_option
    max_rounds = args.max_rounds
    max_challenges = args.max_challenges
    mode = args.mode
    keep_open = args.keep_open
    skip_ids = args.skip
    browser_only = args.browser_only
    extract_only = args.extract_only

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    # 清理可能残留的 flag，避免干扰本次运行
    for _f in (PAUSE_FLAG, EXTRACT_FLAG, AUTOMATION_FLAG, MANUAL_FLAG):
        try:
            _f.unlink()
        except FileNotFoundError:
            pass
    storage_state_path = PROFILE_DIR / "storage-state.json"

    # stdout 同时写到 logs/script_stdout.log，供管理台读取
    _stdout_log = open(LOGS_DIR / "script_stdout.log", "a", buffering=1)

    class _Tee:
        def __init__(self, *files):
            self.files = files

        def write(self, data):
            for f in self.files:
                f.write(data)
                f.flush()

        def flush(self):
            for f in self.files:
                f.flush()

    sys.stdout = _Tee(sys.__stdout__, _stdout_log)
    if mode == "llm":
        has_key = bool(os.getenv("OPENAI_API_KEY", "").strip())
        print(
            f"LLM_CONFIG key={'set' if has_key else 'missing'} "
            f"base={os.getenv('OPENAI_BASE_URL', '') or 'default'} "
            f"model={os.getenv('OPENAI_MODEL', 'deepseek-v4-pro')}",
            flush=True,
        )

    # #region agent log
    _agent_log(
        "A",
        "login_then_select.py:main:start",
        "Script start with unit/group loop",
        {
            "target_url": target_url,
            "seed_option": seed_option,
            "max_rounds": max_rounds,
            "max_challenges": max_challenges,
            "mode": mode,
            "url_kind": classify_start_url(target_url),
        },
    )
    # #endregion

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            handle_sigint=False,
            handle_sigterm=False,
            handle_sighup=False,
            args=[
                "--crash-dumps-dir=/tmp/chrome-crashes",
                "--disable-crashpad",
            ],
        )
        stop_requested = {"value": False}

        def _handle_stop(signum, _frame) -> None:
            stop_requested["value"] = True
            print(f"SIGNAL_{signum}_SAVE_STORAGE", flush=True)
            persist_storage_state(context, storage_state_path, reason=f"signal_{signum}")

        previous_handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[sig] = signal.signal(sig, _handle_stop)
            except Exception:
                pass

        try:
            # Persistent profile alone still bounced to /user_sessions/new in prior runs.
            # Explicitly re-apply storage-state.json like web_agent.BrowserAgent.
            restore_info = restore_storage_state(context, storage_state_path)
            # #region agent log
            _agent_log(
                "LOGIN",
                "login_then_select.py:main:cookies",
                "Restored storage-state before navigation",
                restore_info,
            )
            # #endregion

            page = context.pages[0] if context.pages else context.new_page()
            print("BROWSER_OPENED", flush=True)
            ensure_logged_in(page, target_url, context=context, storage_state_path=storage_state_path)
            print(f"START_PAGE_URL={page.url}", flush=True)

            # browser-only 模式：只开浏览器，等用户手动导航
            if browser_only:
                PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
                PAUSE_FLAG.write_text("1", "utf-8")
                print("BROWSER_READY waiting_for_user_navigation", flush=True)
                check_pause(page)
                print("USER_RESUMED starting", flush=True)
                # 用户手动导航后，用当前页面 URL 替代原始 target_url
                actual_url = page.url
                actual_kind = classify_start_url(actual_url)
                print(
                    f"RESUME_STATE url={actual_url} kind={actual_kind} "
                    f"challenge_id={challenge_id_from_url(actual_url) or 'N/A'}",
                    flush=True,
                )
                if actual_kind in {"challenge", "practice_milestone", "milestone", "catalog"}:
                    target_url = actual_url
                    print(f"TARGET_URL_UPDATED url={target_url}", flush=True)
                else:
                    print(
                        f"TARGET_URL_KEPT url={target_url} "
                        f"(current page kind={actual_kind} not actionable)",
                        flush=True,
                    )

            # ── 统一主循环：run_unit_loop 内部已实现组内/组外循环，不自动暂停 ──
            _rewind_first = False
            _run_extract_only = EXTRACT_FLAG.exists()
            _manual_answers = (
                parse_manual_answers(MANUAL_FLAG.read_text("utf-8")) if MANUAL_FLAG.exists() else None
            )
            if _manual_answers is not None:
                _run_extract_only = False  # 手动模式与提取模式互斥
            # 清理初始 flag
            for _f in (EXTRACT_FLAG, AUTOMATION_FLAG, MANUAL_FLAG):
                try:
                    _f.unlink()
                except Exception:
                    pass

            while not stop_requested["value"]:
                # 检测模式
                if _manual_answers is not None:
                    print(
                        f"MANUAL_MODE activated sequence={''.join(chr(97 + i) for i in _manual_answers)}",
                        flush=True,
                    )
                elif _run_extract_only:
                    print("EXTRACT_ONLY_MODE activated", flush=True)
                else:
                    print("AUTOMATION_MODE activated", flush=True)

                # 从浏览器当前 URL 继续
                actual_url = page.url
                actual_kind = classify_start_url(actual_url)
                if actual_kind in {"challenge", "practice_milestone", "milestone", "catalog"}:
                    target_url = actual_url
                print(f"LOOP_START url={target_url} extract_only={_run_extract_only}", flush=True)

                # 提取完成后切回答题模式：先回退到第一题再开始
                if _rewind_first and not _run_extract_only and _manual_answers is None:
                    rewind_to_first_question(page)
                _rewind_first = False

                unit_reason = run_unit_loop(
                    page,
                    start_url=target_url,
                    mode=mode,
                    seed_option=seed_option,
                    max_rounds=max_rounds,
                    max_challenges=max_challenges,
                    context=context,
                    storage_state_path=storage_state_path,
                    skip_ids=skip_ids,
                    extract_only=_run_extract_only,
                    manual_answers=_manual_answers,
                )

                # 手动模式为一次性：序列消费完即视为本轮结束
                _was_manual = _manual_answers is not None
                _manual_answers = None

                # 提取模式提完最后一题：重新暂停，停在当前页面等用户选择下一个模式
                if (
                    _run_extract_only
                    and unit_reason == "extract_done"
                    and not stop_requested["value"]
                ):
                    PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
                    PAUSE_FLAG.write_text("1", "utf-8")
                    print("EXTRACT_DONE paused at last question; choose next mode in dashboard", flush=True)
                    check_pause(page)
                    # 若接下来切到答题模式，需先回退到第一题
                    if not EXTRACT_FLAG.exists():
                        _rewind_first = True

                # 手动模式答完最后一题：同样重新暂停，可自由切换下一个模式
                if _was_manual and unit_reason == "manual_done" and not stop_requested["value"]:
                    PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
                    PAUSE_FLAG.write_text("1", "utf-8")
                    print("MANUAL_DONE paused at last question; choose next mode in dashboard", flush=True)
                    check_pause(page)

                # 检查是否是 mode_switch 中断（用户手动暂停后切换了模式）
                _switch_to_extract = EXTRACT_FLAG.exists()
                _switch_to_automation = AUTOMATION_FLAG.exists()
                _switch_to_manual = MANUAL_FLAG.exists()
                if _switch_to_manual:
                    _manual_answers = parse_manual_answers(MANUAL_FLAG.read_text("utf-8"))
                    _run_extract_only = False
                    for _f in (EXTRACT_FLAG, AUTOMATION_FLAG, MANUAL_FLAG):
                        try:
                            _f.unlink()
                        except Exception:
                            pass
                    print("MODE_SWITCH detected, switching to manual", flush=True)
                    continue
                if _switch_to_extract or _switch_to_automation:
                    # 用户已经选了新模式，直接切换继续运行
                    _run_extract_only = _switch_to_extract
                    for _f in (EXTRACT_FLAG, AUTOMATION_FLAG):
                        try:
                            _f.unlink()
                        except Exception:
                            pass
                    _new_mode = "extract" if _run_extract_only else "automation"
                    print(f"MODE_SWITCH detected, switching to {_new_mode}", flush=True)
                    continue

                # 没有 mode_switch，run_unit_loop 正常完成，退出主循环
                print("ALL_LOOPS_DONE", flush=True)
                break

            # keep-open: 浏览器保持打开，等待用户手动导航后可重新启动
            if keep_open:
                print(
                    "FLOW_DONE keeping browser open; press Ctrl+C to close",
                    flush=True,
                )
                while not stop_requested["value"]:
                    check_pause(page)
                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        break
        finally:
            for sig, handler in previous_handlers.items():
                try:
                    signal.signal(sig, handler)
                except Exception:
                    pass
            persist_storage_state(context, storage_state_path, reason="finally")
            try:
                context.close()
            except Exception:
                pass
            print("DONE", flush=True)


if __name__ == "__main__":
    main()
