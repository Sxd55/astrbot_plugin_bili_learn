@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Savage Evolution - 上传到 GitHub

REM ==================== 配置区（按需修改） ====================
REM 你的 GitHub 仓库地址。如果你把仓库改名了，这里也要改。
set "REPO_URL=https://github.com/Sxd55/astrbot_plugin_bili_learn.git"
REM 分支名
set "BRANCH=main"
REM ==========================================================

cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 git，请先安装 Git for Windows: https://git-scm.com/download/win
    pause
    exit /b 1
)

if not exist ".git" (
    echo [1/5] 初始化 Git 仓库...
    git init >nul
)
git checkout -b %BRANCH% >nul 2>nul

git config user.name >nul 2>nul
if errorlevel 1 (
    echo [提示] 未设置 Git 提交用户名，使用默认值 Sxd55
    git config user.name "Sxd55"
)
git config user.email >nul 2>nul
if errorlevel 1 (
    echo [提示] 未设置 Git 提交邮箱，使用默认值
    git config user.email "sxd55@users.noreply.github.com"
)

git remote get-url origin >nul 2>nul
if errorlevel 1 (
    echo [2/5] 关联远程仓库: %REPO_URL%
    git remote add origin "%REPO_URL%"
) else (
    echo [2/5] 已关联远程仓库，跳过
)

echo [3/5] 暂存改动...
git add -A

git diff --cached --quiet
if errorlevel 1 (
    for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm'"`) do set "STAMP=%%I"
    if "!STAMP!"=="" set "STAMP=%date% %time:~0,5%"
    echo [4/5] 提交: 更新 !STAMP!
    git commit -m "更新 !STAMP!" >nul
) else (
    echo [4/5] 没有需要提交的改动
)

echo [5/5] 推送到 %BRANCH% ...
git push -u origin %BRANCH%
if errorlevel 1 (
    echo.
    echo [推送失败] 常见原因：
    echo   1. GitHub 上还没有这个仓库：先打开 https://github.com/new 建一个空仓库
    echo   2. 仓库地址不对：编辑本文件顶部的 REPO_URL
    echo   3. 首次推送需要登录：按弹窗提示登录 GitHub 即可
    echo.
    pause
    exit /b 1
)

echo.
echo 上传完成：%REPO_URL%
pause
