# =============================================================================
# sophia-learning Windows 一键环境初始化脚本
#
# 推荐通过双击 setup.bat 运行(自动绕过执行策略限制);也可手动执行:
#     powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
#
# 脚本会依次完成:
#   1. 检查 Python 版本 (需 3.9+, 安装时勾选 Add python.exe to PATH)
#   2. 创建虚拟环境 .venv
#   3. 安装 requirements.txt 中的全部 Python 依赖
#   4. 下载与 playwright==1.60.0 匹配的 Chromium 浏览器
#   5. 从 .env.example 生成 .env 配置模板 (LLM 模式需要填 API Key)
#   6. 创建运行时目录并做 Chromium 启动自检
# =============================================================================

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

function Log($msg)  { Write-Host "[setup] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[setup] $msg" -ForegroundColor Yellow }
function Fail($msg) {
    Write-Host "[setup] $msg" -ForegroundColor Red
    Read-Host "按回车键退出" | Out-Null
    exit 1
}

Log "sophia-learning 环境初始化开始"

# -----------------------------------------------------------------------------
# 1. 查找并检查 Python
# -----------------------------------------------------------------------------
$PyCmd = $null
$PyArgs = @()
foreach ($cmd in @("py", "python", "python3")) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { $PyCmd = $cmd; break }
}
if (-not $PyCmd) {
    Fail "未检测到 Python。请先安装 Python 3.9+: https://www.python.org/downloads/ (安装时务必勾选 Add python.exe to PATH)"
}
if ($PyCmd -eq "py") { $PyArgs = @("-3") }

& $PyCmd @PyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if ($LASTEXITCODE -ne 0) { Fail "Python 版本过低,需要 3.9 及以上" }
$pyVer = & $PyCmd @PyArgs --version 2>&1
Log "检测到 $pyVer"

# -----------------------------------------------------------------------------
# 2. 创建虚拟环境
# -----------------------------------------------------------------------------
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Log "创建虚拟环境 .venv ..."
    & $PyCmd @PyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "创建虚拟环境失败" }
} else {
    Log "虚拟环境 .venv 已存在,跳过创建"
}
& "$PSScriptRoot\.venv\Scripts\Activate.ps1"

# -----------------------------------------------------------------------------
# 3. 安装 Python 依赖
# -----------------------------------------------------------------------------
Log "升级 pip ..."
python -m pip install --upgrade pip --quiet

Log "安装 Python 依赖 (requirements.txt) ..."
pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { Fail "依赖安装失败,请检查网络后重试" }

# -----------------------------------------------------------------------------
# 4. 下载 Playwright Chromium 浏览器
# -----------------------------------------------------------------------------
Log "下载 Chromium 浏览器 (约 170MB,需要联网) ..."
python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Fail "Chromium 下载失败,请检查网络后重试" }
Log "Chromium 下载完成"

# -----------------------------------------------------------------------------
# 5. 生成 .env 配置模板
# -----------------------------------------------------------------------------
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Warn ".env 已从模板生成 —— LLM 模式 (--mode llm) 需要先填入 OPENAI_API_KEY"
    } else {
        Warn ".env 不存在且无模板,LLM 模式不可用 (不影响 manual / extract 模式)"
    }
} else {
    Log ".env 已存在,跳过生成"
}

# -----------------------------------------------------------------------------
# 6. 运行时目录 + 启动自检
# -----------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path "logs", "browser_profiles\sophia" | Out-Null

Log "运行启动自检 ..."
$selfCheck = @'
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
print("[setup] 依赖导入与 Chromium 启动自检全部通过")
'@
python -c $selfCheck
if ($LASTEXITCODE -ne 0) { Fail "启动自检失败" }

# -----------------------------------------------------------------------------
# 完成
# -----------------------------------------------------------------------------
Write-Host ""
Log "环境初始化完成! 使用方法:"
Write-Host @"

  .venv\Scripts\activate

  # 启动可视化答题脚本 (首次会打开浏览器,手动登录 Sophia)
  python scripts\login_then_select.py --browser-only

  # LLM 自动答题模式 (需先在 .env 中配置 OPENAI_API_KEY)
  python scripts\login_then_select.py --mode llm

  # 本地看板
  python scripts\dashboard.py
"@
Write-Host ""
Read-Host "按回车键退出" | Out-Null
