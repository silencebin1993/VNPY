@echo off
rem quant_web installer: creates .venv and installs the packages (run once).
rem NOTE: keep every comment (rem) line ASCII-only and above chcp. After "chcp 65001" cmd.exe
rem mis-reads UTF-8 comment lines and prints "The system cannot find the path specified".
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   量化助手 - 安装运行环境（只需要运行一次）
echo ============================================================

where python >nul 2>nul
if errorlevel 1 goto nopython

if exist ".venv\Scripts\python.exe" goto install
echo.
echo [1/3] 正在创建独立的 Python 环境 .venv ...
python -m venv .venv
if errorlevel 1 goto fail

:install
set "NO_PROXY=pypi.tuna.tsinghua.edu.cn"
set "PY=.venv\Scripts\python.exe"
set "MIRROR=-i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 60"

echo.
echo [2/3] 正在安装依赖（国内清华镜像，约需5-10分钟）...
%PY% -m pip install --upgrade pip %MIRROR%
%PY% -m pip install -e . --no-deps %MIRROR%
if errorlevel 1 goto fail
%PY% -m pip install -r etf_quant\requirements.txt %MIRROR%
if errorlevel 1 (
    echo 镜像安装失败，改用官方源重试...
    %PY% -m pip install -r etf_quant\requirements.txt
    if errorlevel 1 goto fail
)
%PY% -m pip install -r quant_web\requirements.txt %MIRROR%
if errorlevel 1 (
    echo 镜像安装失败，改用官方源重试...
    %PY% -m pip install -r quant_web\requirements.txt
    if errorlevel 1 goto fail
)

echo.
echo [3/3] 检查环境...
%PY% -c "import vnpy.alpha, akshare, etf_quant, fastapi, uvicorn, lightgbm, polars, quant_web; print('OK')"
if errorlevel 1 goto fail

echo.
echo 安装完成！以后双击 "启动量化助手.bat" 即可使用（会自动打开浏览器）。
pause
exit /b 0

:nopython
echo.
echo 没有找到 Python。请先安装 Python 3.10 ~ 3.13：https://www.python.org/downloads/
echo 安装时务必勾选 "Add python.exe to PATH"，装好后重新运行本文件。
pause
exit /b 1

:fail
echo.
echo 安装失败。请检查网络连接后重新运行本文件。
pause
exit /b 1
