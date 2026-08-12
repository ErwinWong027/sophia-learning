# Sophia Browser Control Agent

面向 `https://app.sophia.org` 的 Playwright 网页控制层，采用与原 Bumble Agent 相同的持久化 Chromium profile 机制保存 Cookie 和登录态。不包含聊天、联系人记忆、知识库或数据库。

## 安装与启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
uvicorn app:app --host 127.0.0.1 --port 8000
```

## 执行网页操作

首次运行先打开有界面浏览器并手动登录：

```bash
curl -X POST http://127.0.0.1:8000/agent/run \
  -H 'Content-Type: application/json' \
  -d '{
    "target_url": "https://app.sophia.org",
    "headless": false,
    "keep_open": true,
    "user_data_dir": "./browser_profiles/sophia"
  }'
```

登录完成后调用 `POST /agent/stop`。关闭前 Agent 会把 Cookie 显式导出到 `browser_profiles/sophia/storage-state.json`，并保留 Chromium profile 中的 Local Storage；以后使用同一个 `user_data_dir` 即可恢复登录态。该文件包含登录凭证且已被 Git 忽略，不要分享或提交。

执行操作示例：

```bash
curl -X POST http://127.0.0.1:8000/agent/run \
  -H 'Content-Type: application/json' \
  -d '{
    "target_url": "https://app.sophia.org",
    "headless": false,
    "user_data_dir": "./browser_profiles/sophia",
    "actions": [
      {"operation": "click", "selector": "a"},
      {"operation": "extract_text", "selector": "h1", "name": "title"}
    ]
  }'
```

接口：

- `POST /agent/run`：异步执行操作序列。
- `GET /agent/status`：读取进度、日志和提取结果。
- `POST /agent/stop`：请求停止并关闭浏览器。

支持 `goto`、`click`、`double_click`、`fill`、`type`、`press`、`hover`、`check`、`uncheck`、`select`、`wait_for`、`wait`、`extract_text`、`extract_attribute`、`extract_sophia_question` 和 `screenshot`。

`extract_sophia_question` 会返回当前 Sophia 的 URL、题干和全部选项。题目选择可使用 `click` 操作点击 `#answer-0` 至 `#answer-3`

默认登录目录为 `./browser_profiles/sophia`，该目录已被 Git 忽略。当前控制层支持页面读取、截图与用户确认后的点选，不包含自动代答计分测验。
