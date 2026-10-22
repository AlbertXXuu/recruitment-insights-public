@echo off
setlocal
chcp 65001 >nul
title recruitment-insights - Candidate Preview
set "uiPreviewPython=python"
if exist "%~dp0outputs\invitation-test\python-path.txt" set /p "uiPreviewPython="<"%~dp0outputs\invitation-test\python-path.txt"
"%uiPreviewPython%" -B -c "import pypdf" >nul 2>nul
if errorlevel 1 (
  echo 预览未启动：所选 Python 不可用或缺少 PDF 校验依赖。
  echo 请使用项目配置的 Python，或在该解释器中安装 requirements.txt 后重试。
  pause
  exit /b 1
)
"%uiPreviewPython%" -B -X utf8 "%~dp0scripts\start_ui_preview.py"
if errorlevel 1 pause
