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
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from solver import AnswerRequest, answer_question

PROFILE_DIR = Path(__file__).resolve().parent.parent / "browser_profiles" / "sophia"
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
QUESTION_PATH = LOGS_DIR / "pending_question.json"
ANSWER_PATH = LOGS_DIR / "pending_answer.json"
# #region agent log
DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent / ".cursor" / "debug-65171f.log"
# #endregion

LOGIN_MAX_WAIT = 1200
POLL_SECONDS = 3
SUBMIT_WAIT_MS = 12_000
NEXT_QUESTION_WAIT_MS = 20_000
ANSWER_WAIT_MAX = 900
CHALLENGE_ID_RE = re.compile(r"(challenge-\d+-\d+)", re.I)


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


def ensure_logged_in(page, target_url: str) -> None:
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
    question = page.locator(".question-slide .challenge-v2-question__text").first
    if question.count() == 0:
        return None
    try:
        question.wait_for(state="visible", timeout=5000)
    except Exception:
        return None
    option_labels = page.locator(".question-slide .challenge-v2-answer__input")
    options = [option_labels.nth(i).inner_text().strip() for i in range(option_labels.count())]
    if not options:
        return None
    return {
        "url": page.url,
        "question": question.inner_text().strip(),
        "options": options,
    }


def _normalize_option_text(text: str) -> str:
    return " ".join((text or "").split())


def select_option(page, option_text: str, option_index: int | None = None) -> dict:
    """Select an answer. Prefer stable index / #answer-N; fall back to normalized text match."""
    result: dict = {"method": None, "option_index": option_index, "option_text": option_text}
    options = page.locator(".question-slide .challenge-v2-answer__input")
    options.first.wait_for(state="visible", timeout=10000)
    count = options.count()
    result["option_count"] = count

    # Sophia: click the LABEL (.challenge-v2-answer__input), not #answer-N radio.
    # Runtime evidence: radio click is intercepted by <label for="answer-N">.
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
    # Strip leading "a.)" style markers for looser match
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


def click_submit(page) -> dict:
    """Click Submit My Answer. Prefer enabled button; fall back to force/JS if needed."""
    result: dict = {"attempted": False}
    buttons = page.locator("button", has_text="Submit My Answer")
    if buttons.count() == 0:
        result["reason"] = "button_not_found"
        return result
    btn = buttons.first

    result["attempted"] = True
    result["before"] = _submit_dom_probe(page)
    result["selection"] = _selection_dom_probe(page)
    url_before = page.url
    question_before = None
    extracted = extract_question(page)
    if extracted:
        question_before = extracted.get("question")

    try:
        btn.scroll_into_view_if_needed(timeout=3000)
        result["scrolled"] = True
    except Exception as exc:
        result["scrolled"] = f"fail:{type(exc).__name__}"

    enabled = wait_for_submit_enabled(page, timeout_ms=SUBMIT_WAIT_MS)
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
            # Last resort: DOM click even if CSS class still says disabled
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
    """Return 'challenge' for in-challenge/question URLs, 'catalog' for unit directory."""
    path = urlparse(url).path.rstrip("/")
    if CHALLENGE_ID_RE.search(path):
        return "challenge"
    if "/spcc/" in path:
        return "catalog"
    return "unknown"


def challenge_id_from_url(url: str) -> str | None:
    match = CHALLENGE_ID_RE.search(url or "")
    return match.group(1).lower() if match else None


def unit_catalog_url_from_any(url: str) -> str | None:
    """Derive unit catalog URL from a challenge/question URL when possible."""
    parsed = urlparse(url)
    path = parsed.path
    match = re.search(r"(/spcc/[^/]+?)-challenge-\d+-\d+", path, re.I)
    if match:
        return f"{parsed.scheme}://{parsed.netloc}{match.group(1)}"
    if classify_start_url(url) == "catalog":
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    return None


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


def click_exit_challenge(page) -> dict:
    """Click Exit the Challenge and land on the unit catalog when possible."""
    result: dict = {"attempted": False, "before": probe_exit_and_catalog(page)}
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
            target.click(timeout=8000)
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
    """Probe real Sophia catalog DOM state for each challenge row.

    Runtime-verified signals (logs/catalog_challenge_rows.json):
    - completed: <a class="visited ..."> + span.score-block.ok-score "N/N"
      or svg.svg-attempted-indicator[data-attempted][data-total-questions]
    - locked/unavailable: div.assessment-item-wrapper.show-trial-explanation (not an <a>)
    - available/in_progress: real <a href="...-challenge-..."> without full score
    """
    return page.evaluate(
        """() => {
          const items = Array.from(document.querySelectorAll('.assessment-item.challenge'));
          return items.map((item, index) => {
            const titleEl = item.querySelector('.assessment-item-link-title');
            const title = titleEl ? titleEl.textContent.trim().replace(/\\s+/g, ' ') : '';
            const wrapper = item.closest('a.assessment-item-wrapper, .assessment-item-wrapper');
            const link = item.closest('a.assessment-item-wrapper') || (wrapper && wrapper.tagName === 'A' ? wrapper : null);
            const wrapperClass = wrapper ? String(wrapper.className || '') : '';
            const href = link ? (link.getAttribute('href') || link.getAttribute('data-base-href') || '') : '';
            const showTrialExplanation = /\\bshow-trial-explanation\\b/.test(wrapperClass);
            const visitedClass = /\\bvisited\\b/.test(wrapperClass);
            const isAnchor = !!(link && link.tagName === 'A');
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
            const locked = showTrialExplanation || (!isAnchor && !href);
            let status = 'unknown';
            if (locked) status = 'locked';
            else if (completed) status = 'completed';
            else if (isAnchor && href) status = (attempted != null && attempted > 0) ? 'in_progress' : 'available';
            else status = 'unavailable';

            let challengeId = '';
            const hrefMatch = (href || '').match(/challenge-\\d+-\\d+/i);
            const titleMatch = title.match(/challenge\\s+(\\d+)\\.(\\d+)/i);
            const km = link ? (link.getAttribute('data-km-value') || '') : '';
            const kmMatch = km.match(/challenge-(\\d+)-(\\d+)/i);
            if (hrefMatch) challengeId = hrefMatch[0].toLowerCase();
            else if (kmMatch) challengeId = `challenge-${kmMatch[1]}-${kmMatch[2]}`;
            else if (titleMatch) challengeId = `challenge-${titleMatch[1]}-${titleMatch[2]}`;

            return {
              index,
              title,
              href,
              challengeId,
              status,
              completed,
              locked,
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
            };
          }).filter(item => /challenge/i.test(item.title) || item.challengeId);
        }"""
    )


def enter_next_challenge(page, *, visited: set[str], preferred_catalog_url: str | None = None) -> dict:
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
                    "status": c.get("status"),
                    "scoreText": c.get("scoreText"),
                    "locked": c.get("locked"),
                    "completed": c.get("completed"),
                    "isAnchor": c.get("isAnchor"),
                }
                for c in challenges[:15]
            ],
        },
    )
    # #endregion

    # Pick first actionable challenge in DOM order using real status:
    # skip session-visited, DOM-completed, and locked/unavailable rows.
    pick = None
    for item in challenges:
        cid = (item.get("challengeId") or "").lower()
        if cid and cid in visited:
            continue
        if item.get("completed") or item.get("status") == "completed":
            continue
        if item.get("locked") or item.get("status") in {"locked", "unavailable"}:
            continue
        if item.get("available") or item.get("status") in {"available", "in_progress"}:
            pick = item
            break

    if pick is None:
        # Fallback: first non-completed, non-visited that is at least an anchor link.
        for item in challenges:
            cid = (item.get("challengeId") or "").lower()
            if cid and cid in visited:
                continue
            if item.get("completed"):
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
    compact_title = re.sub(r"\s+", " ", title).strip()
    url_before = page.url
    # Enter by clicking the catalog title only — never synthesize challenge-N-M URLs.
    try:
        by_link = page.locator("a", has=page.locator(".assessment-item-link-title", has_text=compact_title))
        by_title = page.locator(".assessment-item-link-title", has_text=compact_title)
        if by_link.count() > 0:
            by_link.first.scroll_into_view_if_needed(timeout=5000)
            by_link.first.click(timeout=8000)
            result["click_method"] = "title_link"
        elif by_title.count() > 0:
            by_title.first.scroll_into_view_if_needed(timeout=5000)
            by_title.first.click(timeout=8000)
            result["click_method"] = "title_span"
        else:
            result["reason"] = "challenge_title_not_found"
            return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}:{exc}"
        result["reason"] = "title_click_failed"
        return result

    page.wait_for_timeout(2500)
    # Some catalog links open challenge overview; wait for a question if present.
    try:
        page.locator(".question-slide .challenge-v2-question__text, .challenge-v2-answer__input").first.wait_for(
            state="visible", timeout=15000
        )
    except Exception:
        for label in ("Start Challenge", "Resume", "Continue", "Begin"):
            btn = page.locator("button, a", has_text=label)
            try:
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=5000)
                    page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

    cid = challenge_id_from_url(page.url) or (pick.get("challengeId") or "").lower()
    navigated = page.url != url_before or classify_start_url(page.url) == "challenge"
    has_question = extract_question(page) is not None
    if cid and (navigated or has_question):
        visited.add(cid)
    result["clicked"] = {
        "title": title,
        "href": href,
        "challengeId": cid,
        "url_before": url_before,
        "url_after": page.url,
        "navigated": navigated,
    }
    result["ok"] = has_question or classify_start_url(page.url) == "challenge"
    if not result["ok"]:
        # Typical when the account cannot open the next challenge (stay on catalog).
        result["reason"] = "title_clicked_but_still_on_catalog"
        print(
            f"ENTER_CHALLENGE_BLOCKED title={title!r} url={page.url} "
            f"(title click ok; page did not enter challenge — check account access)",
            flush=True,
        )
    else:
        print(f"ENTERED_CHALLENGE title={title!r} url={page.url}", flush=True)
    # #region agent log
    _agent_log(
        "G",
        "login_then_select.py:enter_next_challenge:after_title_click",
        "Catalog title click result (no URL synthesis)",
        {
            "title": title,
            "click_method": result.get("click_method"),
            "ok": result["ok"],
            "reason": result.get("reason"),
            "url_before": url_before,
            "url_after": page.url,
        },
    )
    # #endregion
    return result


def advance_to_next_challenge_group(page, *, visited: set[str], catalog_url: str | None) -> dict:
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

    enter_result = enter_next_challenge(
        page,
        visited=visited,
        preferred_catalog_url=catalog_url or unit_catalog_url_from_any(page.url),
    )
    # #region agent log
    _agent_log(
        "G",
        "login_then_select.py:advance_to_next_challenge_group:enter",
        "Enter next challenge result",
        enter_result,
    )
    # #endregion
    return {"exit": exit_result, "enter": enter_result, "ok": bool(enter_result.get("ok"))}


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
            '{"option_index": 2} or {"option_text": "fixed content"}'
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
) -> dict:
    """Solve questions inside the current challenge. Returns why it stopped."""
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

    for local_round in range(1, max_rounds + 1):
        round_idx = global_round_start + local_round - 1
        page.wait_for_timeout(500)
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
            f"EXTRACTED round={round_idx} question={extracted['question']!r} options={len(extracted['options'])}",
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

        if (not seed_used) and seed_option:
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

        select_result = select_option(
            page,
            option_text,
            option_index=option_index if option_index >= 0 else None,
        )
        page.wait_for_timeout(400)
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

        screenshot_path = LOGS_DIR / f"selected_option_r{round_idx}.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"SCREENSHOT_SAVED {screenshot_path}", flush=True)

        submit_result = click_submit(page)
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

        if str(submit_result.get("click", "")).startswith("fail") or not submit_result.get("attempted"):
            print("SUBMIT_FAILED stopping challenge loop", flush=True)
            status["stop_reason"] = "submit_failed"
            break

        nxt = wait_for_next_question(page, previous_question=extracted["question"])
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

    print(f"CHALLENGE_LOOP_DONE reason={status['stop_reason']}", flush=True)
    return status


def run_unit_loop(
    page,
    *,
    start_url: str,
    mode: str,
    seed_option: str,
    max_rounds: int,
    max_challenges: int,
) -> None:
    """Outer loop: catalog/challenge entry + intra-challenge + Exit + next challenge."""
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
        enter = enter_next_challenge(page, visited=visited, preferred_catalog_url=start_url)
        if not enter.get("ok"):
            print("UNIT_STOP cannot_enter_first_challenge", flush=True)
            return
        catalog_url = catalog_url or start_url.rstrip("/")
    elif url_kind == "challenge":
        cid = challenge_id_from_url(page.url)
        if cid:
            # Do not mark visited until challenge ends; keep for skip-on-reenter only after exit.
            pass
    else:
        print(f"UNIT_STOP unknown_start_url {start_url}", flush=True)
        return

    global_round = 1
    for challenge_idx in range(1, max_challenges + 1):
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
        )
        global_round += int(status.get("rounds_done") or 0)

        if status.get("stop_reason") == "submit_failed":
            print("UNIT_STOP submit_failed", flush=True)
            break

        if challenge_idx >= max_challenges:
            print("UNIT_DONE max_challenges_reached", flush=True)
            break

        if status.get("stop_reason") in {"no_new_question", "no_question", "max_rounds"}:
            advanced = advance_to_next_challenge_group(
                page,
                visited=visited,
                catalog_url=catalog_url,
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
                break
            print(f"GROUP_ADVANCED to={page.url}", flush=True)
            continue

        print(f"UNIT_STOP unexpected_reason={status.get('stop_reason')}", flush=True)
        break

    print("UNIT_LOOP_DONE", flush=True)


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
        default=8,
        help="Max question rounds per challenge (default: 8)",
    )
    parser.add_argument(
        "--max-challenges",
        type=int,
        default=2,
        help="Max challenge groups to traverse (default: 2)",
    )
    parser.add_argument(
        "--mode",
        choices=["ide", "llm"],
        default="ide",
        help="ide=wait for logs/pending_answer.json; llm=LangGraph solver",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    target_url = args.url
    seed_option = args.seed_option
    max_rounds = args.max_rounds
    max_challenges = args.max_challenges
    mode = args.mode

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    storage_state_path = PROFILE_DIR / "storage-state.json"

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
        context = playwright.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False)
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
            ensure_logged_in(page, target_url)
            print(f"START_PAGE_URL={page.url}", flush=True)
            run_unit_loop(
                page,
                start_url=target_url,
                mode=mode,
                seed_option=seed_option,
                max_rounds=max_rounds,
                max_challenges=max_challenges,
            )
        finally:
            # CRITICAL: never overwrite storage-state while on the login page.
            # Prior failed runs saved logged-out cookies and wiped a valid session.
            try:
                page_ref = context.pages[0] if context.pages else None
                can_save = bool(page_ref and looks_logged_in(page_ref))
                # #region agent log
                _agent_log(
                    "LOGIN",
                    "login_then_select.py:main:finally_save",
                    "Decide whether to persist storage-state",
                    {
                        "can_save": can_save,
                        "url": page_ref.url if page_ref else "",
                        "storage_state_path": str(storage_state_path),
                    },
                )
                # #endregion
                if can_save:
                    context.storage_state(path=str(storage_state_path))
                    print("STORAGE_SAVED", flush=True)
                else:
                    print("STORAGE_SKIPPED_NOT_LOGGED_IN", flush=True)
            except Exception as exc:
                print(f"STORAGE_SAVE_FAILED {exc}", flush=True)
            try:
                context.close()
            except Exception:
                pass
            print("DONE", flush=True)


if __name__ == "__main__":
    main()
