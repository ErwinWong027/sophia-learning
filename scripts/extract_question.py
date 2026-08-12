"""One-off script: open a specific Sophia challenge page and extract the
current question text + answer options. Read-only - does not click, select,
or submit any answer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_agent import BrowserAction, BrowserAgent, BrowserAgentConfig

TARGET_URL = sys.argv[1] if len(sys.argv) > 1 else "https://app.sophia.org"

agent = BrowserAgent()
config = BrowserAgentConfig(
    target_url=TARGET_URL,
    headless=True,
    user_data_dir="./browser_profiles/sophia",
    actions=[
        BrowserAction(operation="wait", value="2500"),
        BrowserAction(operation="screenshot", value="./logs/question_check.png", name="question_screenshot"),
        BrowserAction(operation="extract_sophia_question", name="question", timeout_ms=8000),
    ],
)
agent.run(config)
status = agent.wait(60)
print(json.dumps(status, indent=2, ensure_ascii=False))
