#!/usr/bin/env bash
# =============================================================================
# sophia-learning 一键环境初始化脚本
#
# 克隆仓库后执行一次即可:
#     ./setup.sh
#
# 脚本会依次完成:
#   1. 检查 Python 版本 (需 3.9+)
#   2. 创建虚拟环境 .venv
#   3. 安装 requirements.txt 中的全部 Python 依赖
#   4. 下载与 playwright==1.60.0 匹配的 Chromium 浏览器 (Linux 自动装系统依赖)
#   5. macOS: 清除 Chromium 的 quarantine 扩展属性 (否则启动会 SIGSEGV 崩溃)
#   6. 从 .env.example 生成 .env 配置模板 (LLM 模式需要填 API Key)
#   7. 创建运行时目录并做 Chromium 启动自检
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

MIN_PY=(3 9)

log()  { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; exit 1; }

OS_NAME="$(uname -s)"

# -----------------------------------------------------------------------------
# 1. 检查 Python
# -----------------------------------------------------------------------------
PYTHON_BIN="$(command -v python3 || true)"
[ -n "$PYTHON_BIN" ] || die "未找到 python3。macOS 请先执行: brew install python3"

PY_VER="$("$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 版本 $PY_VER 过低,需要 ${MIN_PY[0]}.${MIN_PY[1]} 及以上"
log "Python 检查通过: $PY_VER ($PYTHON_BIN)"

# -----------------------------------------------------------------------------
# 2. 创建虚拟环境
# -----------------------------------------------------------------------------
if [ ! -x .venv/bin/python ]; then
  log "创建虚拟环境 .venv ..."
  "$PYTHON_BIN" -m venv .venv
else
  log "虚拟环境 .venv 已存在,跳过创建"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

log "升级 pip ..."
python -m pip install --upgrade pip --quiet

# -----------------------------------------------------------------------------
# 3. 安装 Python 依赖
# -----------------------------------------------------------------------------
log "安装 Python 依赖 (requirements.txt) ..."
pip install -r requirements.txt --quiet

# -----------------------------------------------------------------------------
# 4. 下载 Playwright Chromium 浏览器
# -----------------------------------------------------------------------------
log "下载 Chromium 浏览器 (约 170MB,需要联网) ..."
if [ "$OS_NAME" = "Linux" ]; then
  # Linux 需要 apt 系统库 (libnss3 等),--with-deps 自动安装
  python -m playwright install --with-deps chromium || {
    warn "自动安装系统依赖失败,请手动执行后重跑本脚本:"
    warn "  sudo python -m playwright install-deps chromium"
    exit 1
  }
else
  python -m playwright install chromium
fi
log "Chromium 下载完成"

# -----------------------------------------------------------------------------
# 5. macOS: 清除 quarantine 扩展属性
#    不清理的话 launch_persistent_context 会因 Crashpad 权限问题 SIGSEGV 崩溃
# -----------------------------------------------------------------------------
if [ "$OS_NAME" = "Darwin" ]; then
  log "macOS: 清理 Chromium quarantine 扩展属性 (防止 Crashpad SIGSEGV) ..."
  PW_CACHE="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/Library/Caches/ms-playwright}"
  APP_PATH="$(find "$PW_CACHE" -maxdepth 4 -name 'Google Chrome for Testing.app' -type d 2>/dev/null | head -n 1 || true)"
  if [ -n "$APP_PATH" ]; then
    xattr -cr "$APP_PATH" 2>/dev/null || warn "xattr 清理失败: $APP_PATH"
  fi
  CRASHPAD_DIR="$HOME/Library/Application Support/Google/Chrome for Testing"
  [ -d "$CRASHPAD_DIR" ] && xattr -cr "$CRASHPAD_DIR" 2>/dev/null || true
  # 本地已有的浏览器 profile 也清理一遍(从压缩包等途径获得时可能带 quarantine 属性)
  [ -d browser_profiles/sophia ] && xattr -cr browser_profiles/sophia 2>/dev/null || true
  log "quarantine 清理完成"
fi

# -----------------------------------------------------------------------------
# 6. 生成 .env 配置模板
# -----------------------------------------------------------------------------
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    warn ".env 已从模板生成 —— LLM 模式 (--mode llm) 需要先填入 OPENAI_API_KEY"
  else
    warn ".env 不存在且无模板,LLM 模式不可用 (不影响 manual / extract 模式)"
  fi
else
  log ".env 已存在,跳过生成"
fi

# -----------------------------------------------------------------------------
# 7. 运行时目录 + 启动自检
# -----------------------------------------------------------------------------
mkdir -p logs browser_profiles/sophia

log "运行启动自检 ..."
python - <<'PY'
import importlib

for mod in ("fastapi", "uvicorn", "langgraph", "openai", "playwright"):
    importlib.import_module(mod)

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content("<h1>setup ok</h1>")
    assert "setup ok" in page.content()
    browser.close()
print("依赖导入与 Chromium 启动自检全部通过")
PY

# -----------------------------------------------------------------------------
# 完成
# -----------------------------------------------------------------------------
echo ""
log "🎉 环境初始化完成! 使用方法:"
cat <<'EOF'

  source .venv/bin/activate

  # 启动可视化答题脚本 (首次会打开浏览器,手动登录 Sophia)
  python scripts/login_then_select.py --browser-only

  # LLM 自动答题模式 (需先在 .env 中配置 OPENAI_API_KEY)
  python scripts/login_then_select.py --mode llm

  # 本地看板
  python scripts/dashboard.py
EOF
