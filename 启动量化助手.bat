@echo off
rem quant_web launcher: starts the local web app and opens the browser; close this window to quit.
rem The old ETF command-line menu still works: .venv\Scripts\python.exe -m etf_quant
rem (or pass the argument etf to this file).
rem NOTE: keep every comment (rem) line ASCII-only and above chcp. After "chcp 65001" cmd.exe
rem mis-reads UTF-8 comment lines and prints "The system cannot find the path specified".
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto noenv
set "PYTHONIOENCODING=utf-8"
.venv\Scripts\python.exe -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 goto noweb
title 量化助手（网页版）- 关闭本窗口即退出
.venv\Scripts\python.exe -m quant_web %*
set "CODE=%errorlevel%"
if not "%CODE%"=="0" (
    echo.
    echo 网页版启动失败。可以先使用旧版命令行菜单：.venv\Scripts\python.exe -m etf_quant
    pause
)
exit /b %CODE%

:noenv
echo 第一次使用，请先双击运行 "安装环境.bat"。
pause
exit /b 1

:noweb
echo 网页版需要的组件还没有安装，请先双击运行 "安装环境.bat"（会自动补装）。
echo 在此之前也可以使用旧版命令行菜单：.venv\Scripts\python.exe -m etf_quant
pause
exit /b 1
