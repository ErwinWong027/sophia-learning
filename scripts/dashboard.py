#!/usr/bin/env python3
"""Sophia 脚本管理台 — 通过浏览器控制 login_then_select.py 的启动/停止/日志。

用法: .venv/bin/python scripts/dashboard.py [端口号]
默认端口 7777，打开 http://localhost:7777
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = str(ROOT / ".venv" / "bin" / "python")
SCRIPT_PATH = str(ROOT / "scripts" / "login_then_select.py")
LOGS_DIR = ROOT / "logs"
DONE_PM_PATH = LOGS_DIR / "done_practice_milestones.json"
FRONTEND_DIR = Path(__file__).resolve().parent / "dashboard"
PAUSE_FLAG = LOGS_DIR / "pause.flag"
EXTRACT_FLAG = LOGS_DIR / "extract_only.flag"
AUTOMATION_FLAG = LOGS_DIR / "automation.flag"
MANUAL_FLAG = LOGS_DIR / "manual_answers.flag"


def pause_script() -> dict:
    """暂停脚本（创建 pause.flag）。"""
    PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FLAG.write_text("1", "utf-8")
    _append_log("[dashboard] 暂停信号已发送")
    return {"ok": True, "paused": True}


def resume_script() -> dict:
    """恢复脚本（删除 pause.flag）。"""
    try:
        PAUSE_FLAG.unlink()
    except FileNotFoundError:
        pass
    _append_log("[dashboard] 恢复信号已发送")
    return {"ok": True, "resumed": True}


def extract_only_resume() -> dict:
    """创建 extract_only 信号并恢复脚本。"""
    EXTRACT_FLAG.parent.mkdir(parents=True, exist_ok=True)
    EXTRACT_FLAG.write_text("1", "utf-8")
    # 清理 automation flag（互斥）
    try:
        AUTOMATION_FLAG.unlink()
    except FileNotFoundError:
        pass
    try:
        PAUSE_FLAG.unlink()
    except FileNotFoundError:
        pass
    _append_log("[dashboard] 提取题目模式已激活")
    return {"ok": True, "extract_only": True}


def automation_resume() -> dict:
    """创建 automation 信号并恢复脚本（从 extract 切换到自动化）。"""
    AUTOMATION_FLAG.parent.mkdir(parents=True, exist_ok=True)
    AUTOMATION_FLAG.write_text("1", "utf-8")
    # 清理 extract flag（互斥）
    try:
        EXTRACT_FLAG.unlink()
    except FileNotFoundError:
        pass
    try:
        PAUSE_FLAG.unlink()
    except FileNotFoundError:
        pass
    _append_log("[dashboard] 自动化模式已激活")
    return {"ok": True, "automation": True}


def manual_resume(answers: str) -> dict:
    """写入手动答案序列（如 abcd）并恢复脚本，依次选择后停止。"""
    seq = [c for c in (answers or "").lower() if c in "abcd"]
    if not seq:
        return {"ok": False, "error": "答案序列为空，请输入 a-d 的字母，如 abcd"}
    MANUAL_FLAG.parent.mkdir(parents=True, exist_ok=True)
    MANUAL_FLAG.write_text("".join(seq), "utf-8")
    # 清理互斥 flag
    for _f in (EXTRACT_FLAG, AUTOMATION_FLAG):
        try:
            _f.unlink()
        except FileNotFoundError:
            pass
    try:
        PAUSE_FLAG.unlink()
    except FileNotFoundError:
        pass
    _append_log(f"[dashboard] 手动答案模式已激活: {''.join(seq)}")
    return {"ok": True, "manual": True, "sequence": "".join(seq)}

# ── 全局状态 ──────────────────────────────────────────────
_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()
_log_buffer: deque[str] = deque(maxlen=2000)
_log_lock = threading.Lock()
_reader_thread: threading.Thread | None = None


def _append_log(line: str) -> None:
    ts = time.strftime("%H:%M:%S")
    with _log_lock:
        _log_buffer.append(f"[{ts}] {line}")


def _reader_loop() -> None:
    """后台线程：持续读取子进程 stdout 并写入环形缓冲区。"""
    global _proc
    if _proc is None or _proc.stdout is None:
        return
    try:
        for raw in iter(_proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
            if line:
                _append_log(line)
    except Exception:
        pass
    _append_log(f"[reader] 进程输出流结束, returncode={_proc.poll()}")


# ── 进程管理 ──────────────────────────────────────────────
def start_script(params: dict) -> dict:
    """启动 login_then_select.py 子进程。"""
    global _proc, _reader_thread
    with _proc_lock:
        if _proc is not None and _proc.poll() is None:
            return {"ok": False, "error": "脚本已在运行中"}

        # 检测外部进程
        ext_pid = _find_external_pid()
        if ext_pid:
            return {"ok": False, "error": f"外部脚本运行中 (PID: {ext_pid})，请先停止"}

        url = params.get("url", "https://app.sophia.org/spcc/introduction-to-relational-databases-2")
        mode = params.get("mode", "llm")
        max_rounds = params.get("max_rounds", 999)
        max_challenges = params.get("max_challenges", 999)
        skip = params.get("skip", "")
        browser_only = params.get("browser_only", False)
        extract_only = params.get("extract_only", False)

        cmd = [VENV_PYTHON, SCRIPT_PATH, url]
        if mode:
            cmd += ["--mode", mode]
        cmd += ["--max-rounds", str(max_rounds)]
        cmd += ["--max-challenges", str(max_challenges)]
        if skip:
            cmd += ["--skip", skip]
        if browser_only:
            cmd += ["--browser-only"]
        if extract_only:
            cmd += ["--extract-only"]

        _append_log(f"[dashboard] 启动: {' '.join(cmd)}")
        try:
            _proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(ROOT),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                start_new_session=True,  # 独立进程组，SIGINT 不会互相传播
            )
        except Exception as exc:
            _append_log(f"[dashboard] 启动失败: {exc}")
            return {"ok": False, "error": str(exc)}

        _reader_thread = threading.Thread(target=_reader_loop, daemon=True)
        _reader_thread.start()
        return {"ok": True, "pid": _proc.pid}


def stop_script() -> dict:
    """停止脚本（SIGINT → 关闭浏览器）。"""
    global _proc
    # 先清除暂停标志
    try:
        PAUSE_FLAG.unlink()
    except FileNotFoundError:
        pass

    with _proc_lock:
        if _proc is not None and _proc.poll() is None:
            pid = _proc.pid
            _append_log(f"[dashboard] SIGINT pid={pid}")
            try:
                _proc.send_signal(signal.SIGINT)
                _proc.wait(timeout=15)
            except Exception:
                try:
                    _proc.kill()
                    _proc.wait(timeout=5)
                except Exception:
                    pass
            _proc = None
            _append_log("[dashboard] 进程已停止")
            return {"ok": True}

        _proc = None

    ext_pid = _find_external_pid()
    if ext_pid:
        _append_log(f"[dashboard] SIGINT 外部进程 pid={ext_pid}")
        try:
            os.kill(ext_pid, signal.SIGINT)
            time.sleep(5)
            if _find_external_pid():
                os.kill(ext_pid, signal.SIGKILL)
                time.sleep(1)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        _append_log("[dashboard] 外部进程已停止")
        return {"ok": True}

    return {"ok": True, "already_stopped": True}


def kill_script() -> dict:
    """强制杀进程（SIGKILL），会关闭浏览器。"""
    global _proc
    with _proc_lock:
        if _proc is not None and _proc.poll() is None:
            pid = _proc.pid
            _append_log(f"[dashboard] SIGKILL pid={pid}")
            try:
                _proc.kill()
                _proc.wait(timeout=5)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            _proc = None
            _append_log("[dashboard] 进程已强制终止")
            return {"ok": True}

        _proc = None

    ext_pid = _find_external_pid()
    if ext_pid:
        _append_log(f"[dashboard] SIGKILL 外部进程 pid={ext_pid}")
        try:
            os.kill(ext_pid, signal.SIGKILL)
            time.sleep(1)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        _append_log("[dashboard] 外部进程已强制终止")
        return {"ok": True}

    return {"ok": True, "already_stopped": True}


def _find_external_pid() -> int | None:
    """用 pgrep 查找正在运行的 login_then_select.py 进程 PID（排除自身）。"""
    try:
        output = subprocess.check_output(
            ["pgrep", "-f", "login_then_select\\.py"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        pids = [int(p) for p in output.split("\n") if p.strip()]
        pids = [p for p in pids if p != os.getpid()]
        return pids[0] if pids else None
    except Exception:
        return None


def _read_stdout_log() -> list[str]:
    """读取 logs/script_stdout.log 的最后 150 行。"""
    log_file = LOGS_DIR / "script_stdout.log"
    try:
        lines = log_file.read_text("utf-8", errors="replace").splitlines()
        return lines[-150:]
    except Exception:
        return []


def get_reports() -> dict:
    """获取 logs 目录下所有报告文件（MD + JSON，排除内部状态文件）。"""
    reports = []
    # 内部状态文件，不展示
    _internal = {
        "pause.flag", "extract_only.flag", "script_stdout.log",
        "pending_question.json", "pending_answer.json",
        "done_practice_milestones.json",
    }
    try:
        for f in sorted(LOGS_DIR.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if not f.is_file():
                continue
            if f.name in _internal:
                continue
            # 只展示 md / json / png
            if f.suffix.lower() not in (".md", ".json", ".png"):
                continue
            kind = "image" if f.suffix.lower() == ".png" else f.suffix.lower().lstrip(".")
            reports.append({
                "name": f.name,
                "path": str(f),
                "kind": kind,
                "size": f.stat().st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime)),
            })
    except Exception:
        pass
    return {"reports": reports}


def get_report_content(name: str) -> dict:
    """获取指定报告文件的内容（MD/JSON 文本，PNG 返回路径）。"""
    report_path = LOGS_DIR / name
    # 安全检查：确保路径在 logs 目录下
    if not report_path.exists() or not str(report_path.resolve()).startswith(str(LOGS_DIR.resolve())):
        return {"error": "文件不存在"}
    try:
        if report_path.suffix.lower() == ".png":
            return {"name": name, "kind": "image", "path": str(report_path)}
        content = report_path.read_text("utf-8", errors="replace")
        kind = "md" if report_path.suffix.lower() == ".md" else "json"
        return {"name": name, "kind": kind, "content": content}
    except Exception as exc:
        return {"error": str(exc)}


def delete_report(name: str) -> dict:
    """删除指定的报告文件。"""
    report_path = LOGS_DIR / name
    if not report_path.exists() or not str(report_path.resolve()).startswith(str(LOGS_DIR.resolve())):
        return {"ok": False, "error": "文件不存在"}
    try:
        report_path.unlink()
        return {"ok": True, "deleted": name}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def clear_all_reports() -> dict:
    """清空 logs 目录下所有报告/输出文件（保留 flag 和 stdout log）。"""
    _protected = {"pause.flag", "extract_only.flag", "script_stdout.log"}
    deleted = []
    try:
        for f in LOGS_DIR.iterdir():
            if f.is_file() and f.name not in _protected:
                f.unlink()
                deleted.append(f.name)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "deleted": deleted}
    return {"ok": True, "deleted": deleted}


def get_status() -> dict:
    """获取当前脚本状态和最近日志。"""
    with _proc_lock:
        running = _proc is not None and _proc.poll() is None
        pid = _proc.pid if _proc else None
        external = False

    # 如果管理台没启动脚本，检测外部进程
    if not running:
        ext_pid = _find_external_pid()
        if ext_pid:
            running = True
            pid = ext_pid
            external = True

    # 日志优先用管理台自己的 buffer，没有就读 stdout 日志文件
    with _log_lock:
        logs = list(_log_buffer)
    if not logs:
        logs = _read_stdout_log()

    done_pm: list[str] = []
    try:
        done_pm = json.loads(DONE_PM_PATH.read_text("utf-8"))
    except Exception:
        pass

    return {
        "running": running,
        "pid": pid,
        "external": external,
        "paused": PAUSE_FLAG.exists(),
        "logs": logs[-150:],
        "done_practice_milestones": done_pm,
    }


# ── HTTP Handler ──────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默 HTTP 访问日志

    def _json(self, data: dict, code: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content: str):
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html_path = FRONTEND_DIR / "index.html"
            try:
                self._html(html_path.read_text("utf-8"))
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
        elif self.path == "/api/status":
            self._json(get_status())
        elif self.path == "/api/reports":
            self._json(get_reports())
        elif self.path.startswith("/api/report/"):
            name = self.path[len("/api/report/"):]
            self._json(get_report_content(name))
        elif self.path == "/api/reports/clear":
            self._json(clear_all_reports())
        else:
            self._json({"error": "not_found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw)
        except Exception:
            body = {}

        if self.path == "/api/start":
            self._json(start_script(body))
        elif self.path == "/api/stop":
            self._json(stop_script())
        elif self.path == "/api/kill":
            self._json(kill_script())
        elif self.path == "/api/pause":
            self._json(pause_script())
        elif self.path == "/api/resume":
            self._json(resume_script())
        elif self.path == "/api/extract":
            self._json(extract_only_resume())
        elif self.path == "/api/automation":
            self._json(automation_resume())
        elif self.path == "/api/manual":
            self._json(manual_resume(str(body.get("answers", ""))))
        elif self.path == "/api/reports/delete":
            name = body.get("name", "")
            self._json(delete_report(name))
        else:
            self._json({"error": "not_found"}, 404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7777
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"📊 管理台已启动: http://localhost:{port}")
    print(f"   控制脚本: {SCRIPT_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n管理台关闭中...")
        stop_script()
        server.shutdown()


if __name__ == "__main__":
    main()
