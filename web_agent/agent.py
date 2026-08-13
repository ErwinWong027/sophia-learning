from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BrowserAction:
    operation: str
    selector: str = ""
    value: str = ""
    url: str = ""
    key: str = ""
    name: str = ""
    attribute: str = ""
    timeout_ms: int = 10_000
    delay_ms: int = 0


@dataclass(frozen=True)
class BrowserAgentConfig:
    target_url: str
    actions: list[BrowserAction] = field(default_factory=list)
    headless: bool = False
    user_data_dir: str = "./browser_profiles/sophia"
    keep_open: bool = False
    default_timeout_ms: int = 10_000


class BrowserAgent:
    """独立 Playwright 网页操作 Agent，使用持久化 Chromium profile 保存登录态。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status: dict[str, Any] = self._initial_status()

    def run(self, config: BrowserAgentConfig) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return dict(self._status)
            self._stop.clear()
            self._status = self._initial_status()
            self._status.update({"running": True, "stage": "STARTING", "total_actions": len(config.actions)})
            self._thread = threading.Thread(target=self._loop, args=(config,), daemon=True)
            self._thread.start()
            return dict(self._status)

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        with self._lock:
            if self._status["running"]:
                self._status["stage"] = "STOPPING"
            return dict(self._status)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._status,
                "results": dict(self._status["results"]),
                "logs": list(self._status["logs"]),
            }

    def wait(self, timeout: float | None = None) -> dict[str, Any]:
        thread = self._thread
        if thread:
            thread.join(timeout)
        return self.status()

    @staticmethod
    def _initial_status() -> dict[str, Any]:
        return {
            "running": False,
            "stage": "IDLE",
            "current_action": -1,
            "completed_actions": 0,
            "total_actions": 0,
            "last_error": "",
            "results": {},
            "logs": [],
        }

    def _loop(self, config: BrowserAgentConfig) -> None:
        browser = None
        context = None
        storage_state_path: Path | None = None
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                self._set_stage("LAUNCHING", "启动 Chromium")
                if config.user_data_dir:
                    profile_dir = Path(config.user_data_dir).expanduser().resolve()
                    profile_dir.mkdir(parents=True, exist_ok=True)
                    storage_state_path = profile_dir / "storage-state.json"
                    context = playwright.chromium.launch_persistent_context(
                        str(profile_dir), headless=config.headless
                    )
                    self._restore_storage_state(context, storage_state_path)
                else:
                    browser = playwright.chromium.launch(headless=config.headless)
                    context = browser.new_context()

                context.set_default_timeout(config.default_timeout_ms)
                page = context.pages[0] if context.pages else context.new_page()
                self._set_stage("NAVIGATING", f"打开 {config.target_url}")
                page.goto(config.target_url, wait_until="domcontentloaded")

                for index, action in enumerate(config.actions):
                    if self._stop.is_set():
                        break
                    with self._lock:
                        self._status["current_action"] = index
                        self._status["stage"] = "RUNNING"
                    result = self._execute(page, action)
                    if result is not None:
                        result_name = action.name or f"action_{index}"
                        with self._lock:
                            self._status["results"][result_name] = result
                    with self._lock:
                        self._status["completed_actions"] = index + 1
                    self._log(f"已执行 {index + 1}/{len(config.actions)}: {action.operation}")

                if config.keep_open and not self._stop.is_set():
                    self._set_stage("WAITING", "操作完成，浏览器保持打开")
                    while not self._stop.wait(0.2):
                        pass

                final_stage = "STOPPED" if self._stop.is_set() else "FINISHED"
                self._set_stage(final_stage, "浏览器任务结束")
                if storage_state_path is not None:
                    context.storage_state(path=str(storage_state_path))
                    self._log("已保存持久化 Cookie")
        except Exception as exc:
            with self._lock:
                self._status["stage"] = "ERROR"
                self._status["last_error"] = str(exc)
            self._log(f"错误: {exc}")
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            with self._lock:
                self._status["running"] = False

    def _restore_storage_state(self, context, storage_state_path: Path) -> None:
        if not storage_state_path.exists():
            return
        try:
            state = json.loads(storage_state_path.read_text(encoding="utf-8"))
            cookies = state.get("cookies", [])
            if cookies:
                context.add_cookies(cookies)
                self._log("已恢复持久化 Cookie")
        except Exception as exc:
            self._log(f"Cookie 恢复失败，将使用 Chromium profile: {exc}")

    def _execute(self, page, action: BrowserAction) -> Any:
        operation = action.operation.lower().strip()
        timeout = action.timeout_ms

        if operation == "goto":
            response = page.goto(action.url or action.value, wait_until="domcontentloaded", timeout=timeout)
            return response.status if response else None
        if operation == "click":
            page.locator(action.selector).click(timeout=timeout)
            return None
        if operation == "double_click":
            page.locator(action.selector).dblclick(timeout=timeout)
            return None
        if operation == "fill":
            page.locator(action.selector).fill(action.value, timeout=timeout)
            return None
        if operation == "type":
            page.locator(action.selector).press_sequentially(action.value, delay=action.delay_ms, timeout=timeout)
            return None
        if operation == "press":
            page.locator(action.selector).press(action.key or action.value, timeout=timeout)
            return None
        if operation == "hover":
            page.locator(action.selector).hover(timeout=timeout)
            return None
        if operation == "check":
            page.locator(action.selector).check(timeout=timeout)
            return None
        if operation == "uncheck":
            page.locator(action.selector).uncheck(timeout=timeout)
            return None
        if operation == "select":
            page.locator(action.selector).select_option(action.value, timeout=timeout)
            return None
        if operation == "wait_for":
            page.locator(action.selector).wait_for(state=action.value or "visible", timeout=timeout)
            return None
        if operation == "wait":
            page.wait_for_timeout(min(max(int(action.value), 0), 60_000))
            return None
        if operation == "extract_text":
            return page.locator(action.selector).inner_text(timeout=timeout)
        if operation == "extract_attribute":
            if not action.attribute:
                raise ValueError("extract_attribute 需要 attribute")
            return page.locator(action.selector).get_attribute(action.attribute, timeout=timeout)
        if operation == "extract_sophia_question":
            question = page.locator(".question-slide .challenge-v2-question__text").first
            question.wait_for(state="visible", timeout=timeout)
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
                return { question, options };
            }""")
            return {
                "url": page.url,
                "question": data.get("question", ""),
                "options": data.get("options", []),
            }
        if operation == "screenshot":
            output = Path(action.value).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output), full_page=True)
            return str(output)
        raise ValueError(f"不支持的操作: {action.operation}")

    def _set_stage(self, stage: str, message: str) -> None:
        with self._lock:
            self._status["stage"] = stage
        self._log(message)

    def _log(self, message: str) -> None:
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "message": message}
        with self._lock:
            self._status["logs"] = (self._status["logs"] + [entry])[-200:]
