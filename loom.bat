@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title Loom Agent v0.1a

REM ==========================================================================
REM  Loom Agent v0.1a  ·  一键启动器
REM --------------------------------------------------------------------------
REM  用法：双击本文件，然后按提示输入工作目录。
REM
REM  为什么需要这个脚本（手敲命令要记 4 件事）：
REM    1. 必须先 cd 到 loom 根目录，否则 python -m backend.cli 找不到包
REM    2. 必须 unset 系统代理，否则请求会被本机代理拦住，LLM 调用直接超时
REM    3. 必须用 .venv 里的 python，不是系统的
REM    4. 工作区路径要每次换
REM  这四条错一条就跑不起来，而报错信息各不相同 —— 所以固化进脚本。
REM
REM  本文件必须放在 loom 根目录（与 backend\ 、.venv\ 同级）。
REM ==========================================================================

REM ---- 0. 结束后是否暂停（默认暂停，方便双击后还能读到输出）--------------
REM  ★ 这里原本写的是 `echo %cmdcmdline% | find /i "/c"` —— 想用它判断
REM    「是不是从资源管理器双击启动的」，只有双击才 pause。
REM
REM    结果实测会把整个脚本**挂死**：`find` 在等 stdin 关闭，
REM    而管道在非交互环境下等不到 EOF，整行永远不返回。
REM    （症状：脚本一个字都不输出就卡住，连前几行的 echo 都看不到 ——
REM      因为 cmd 是逐行执行的，卡住的行之前的内容虽然已执行，
REM      但进程整个僵在那里，看起来像「一启动就死」。）
REM
REM    改成显式参数，零魔法、可预测：脚本调用时传 --no-pause 即可。
set "NOPAUSE="
if /i "%~1"=="--no-pause" set "NOPAUSE=1"

REM ---- 1. 切到脚本所在目录 = loom 根 --------------------------------------
cd /d "%~dp0"

REM ---- 2. 清除系统代理 ----------------------------------------------------
REM  本机系统代理是注册表级设置，会拦所有 HTTP(S) 请求。
REM  不清理的话，CLI 会卡在「正在调用模型…」直到超时。
set "http_proxy="
set "https_proxy="
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "all_proxy="

REM ---- 2b. 清除开发环境注入的 Python 编码变量（★ 不清理会直接崩）---------
REM
REM  实测：本机环境里带着这两个变量（由开发工具链注入）——
REM      PYTHONUTF8=1
REM      PYTHONIOENCODING=utf-8
REM
REM  它们让 Python 按 UTF-8 读写标准输入输出，并**绕过 Windows 控制台的
REM  原生 Unicode 通道**（PEP 528），退回「按指定编码读写字节」的老实现。
REM  而 cmd 窗口的代码页是 GBK（936），于是：用户在提示符下敲一句中文任务
REM      → stdin 送来 GBK 字节（如 0xd5 0xe2 ...）
REM      → Python 按 UTF-8 解码
REM      → UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd5
REM      → 整个 CLI 带着 traceback 崩掉
REM
REM  清掉之后 Python 回到默认路径：控制台输入走原生 Unicode 读取，
REM  与代码页无关，中文输入输出都正常。
REM  （另：Loom 的工具层读写文件都显式指定了 encoding="utf-8"，
REM    所以清掉这两个变量不会影响文件操作的编码。）
set "PYTHONUTF8="
set "PYTHONIOENCODING="
set "PYTHONLEGACYWINDOWSSTDIO="

REM ---- 3. 环境体检 --------------------------------------------------------
set "PY=.venv\Scripts\python.exe"
set "LASTFILE=.loom_last_workspace"

if not exist "%PY%" (
    echo.
    echo   [X] 找不到虚拟环境：%CD%\%PY%
    echo.
    echo       本脚本必须放在 loom 根目录，且已经建好 .venv。
    echo       如果你把脚本挪到了别处，请放回去再运行。
    echo.
    goto done
)

if not exist ".env" (
    echo.
    REM ★ 这里用 [提示] 而不是 [!] —— 在 enabledelayedexpansion 下，
    REM   echo 里的**孤立** `!` 会被当成延迟展开符号，把它后面一大段
    REM   文字整个吃掉。实测：`[!] 还没有历史工作目录，请输入一个路径
    REM   （例如 D:\` 输出成了 `[\` —— 提示语断了一半，查半天才发现。
    REM   配对的 !VAR! 是安全的，危险的是落单的那个 `!`。
    echo   [提示] 找不到 .env —— 没有 API Key 就跑不了任务。
    echo.
    echo       请复制 .env.example 为 .env，填入：
    echo         BAILIAN_API_KEY=sk-xxxxxxxx
    echo.
    goto done
)

REM 读一下当前用哪个模型（只为了在横幅里显示，读不到就留空）
set "PROVIDER="
for /f "tokens=2 delims==" %%a in ('findstr /b /c:"LOOM_DEFAULT_MODEL=" .env 2^>nul') do set "PROVIDER=%%a"

REM ---- 4. 抬头 ------------------------------------------------------------
echo.
echo ══════════════════════════════════════════════════════════════════════════
echo   Loom Agent v0.1a   ·  启动器
echo ──────────────────────────────────────────────────────────────────────────
echo   Python      %PY%
echo   配置文件    .env
if defined PROVIDER echo   默认模型    %PROVIDER%
echo   代理        已清除
echo ══════════════════════════════════════════════════════════════════════════
echo.

REM ---- 5. 问工作目录 ------------------------------------------------------
echo   [工作目录]
echo   Agent 只能在这个目录里活动，越界会被硬拒绝。
echo   可以直接把文件夹拖进这个窗口；直接回车则用上次的目录。
echo.

REM 读上次的选择。
REM ★ 用 goto 而不是 if(...) 括号块包住 —— 括号块里的 `<file` 重定向
REM   在部分 cmd 版本下会污染后续命令的 stdin（排查时踩过这个坑）。
set "WORKDIR="
if not exist "%LASTFILE%" goto no_last
set /p WORKDIR=<"%LASTFILE%"
:no_last

REM ★ 历史目录必须校验：它可能已被删除 / 改名 / 换了盘符。
REM   不校验的话，用户一回车「复用」就会拿到一个无效工作区，
REM   Agent 一启动就在一个不存在的地方活动，错误要拖到第一次读文件才暴露。
if defined WORKDIR (
    if not exist "!WORKDIR!\" (
        echo   [提示] 上次的工作目录已不可用（不存在或不是目录）：
        echo       !WORKDIR!
        set "WORKDIR="
    )
)

if defined WORKDIR echo   上次使用：!WORKDIR!
echo.

set /a TRIES=0

:ask_dir
set "INPUT="
set /p "INPUT=  请输入路径: "

REM ★★ 这里绝不能再用 errorlevel 判断「读不到输入」—— 旧版的 bug 就在这。
REM    实测结论（dev 里的探针脚本验证过）：
REM      set /p 在「用户直接敲回车」和「stdin 已关闭」两种情况下表现
REM      **完全相同** —— 变量保持原值不变 + errorlevel=1。
REM    所以拿 errorlevel 判断，就等于把「回车复用上次目录」误判成
REM    「stdin 结束」然后直接退出 —— 用户永远用不了回车复用。
REM
REM    正确做法：只看**有没有读到内容**。没读到就按「空输入」处理，
REM    交给 last_workspace 决定是复用还是重新问；
REM    真正的死循环（非交互环境里连问 N 次都读不到）由 TRIES 兜底。
if not defined INPUT goto no_input

REM ---- 读到内容 → 去掉拖拽产生的引号和前导空格，再校验 ----
set INPUT=!INPUT:"=!
for /f "tokens=* delims= " %%a in ("!INPUT!") do set "INPUT=%%a"

if not exist "!INPUT!\" (
    set /a TRIES+=1
    echo.
    echo   [X] 目录不存在：!INPUT!
    if !TRIES! gtr 5 goto too_many_tries
    goto ask_dir
)

REM 转成绝对路径（相对路径按当前目录 = loom 根解析）
for %%i in ("!INPUT!") do set "WORKDIR=%%~fi"
goto check_dir

:no_input
REM ---- 空输入（回车 / stdin 关闭）：有历史就复用，没有就提示重问 ----
if defined WORKDIR (
    echo   -^> 沿用上次：!WORKDIR!
    goto check_dir
)
set /a TRIES+=1
if !TRIES! gtr 5 goto too_many_tries
echo   [提示] 还没有历史工作目录，请输入一个路径（例如 D:\Projects\Test）。
echo.
goto ask_dir

:too_many_tries
echo.
echo   [X] 连续 !TRIES! 次没拿到可用路径，退出。
echo       提示：Agent 必须有一个工作目录才能启动 ——
echo       它只能在这个目录里读写文件，越界会被硬拒绝。
goto done

:check_dir
if not exist "%WORKDIR%\" (
    echo   [X] 目录不存在：%WORKDIR%
    set "WORKDIR="
    goto ask_dir
)

REM 记住这次的选择
echo %WORKDIR%>"%LASTFILE%"

REM ---- 6. 问额外参数（可跳过）--------------------------------------------
echo.
echo   [额外参数]  直接回车 = 普通模式（写入需要你逐条批准）
echo     --readonly   只读模式，卸载所有写入工具，零副作用
echo     --compact    精简输出，只看骨架
echo     --no-think   不显示思维链，只看动作和结论
echo     --max-turns 30   放宽轮次上限（长任务用）
echo.
set "EXTRA="
set /p "EXTRA=  参数（可留空） > "

REM ---- 7. 起 ------------------------------------------------------------------
echo.
echo ──────────────────────────────────────────────────────────────────────────
echo   工作区   %WORKDIR%
echo   参数     %EXTRA%
echo ──────────────────────────────────────────────────────────────────────────
echo.
echo   进入后：输入任务回车执行  ^|  :t 看工具  ^|  :s 看预算  ^|  :q 退出
echo.

"%PY%" -m backend.cli -w "%WORKDIR%" %EXTRA%

REM ---- 8. 收尾 ------------------------------------------------------------
echo.
echo ──────────────────────────────────────────────────────────────────────────
echo   Loom 已退出。
echo ──────────────────────────────────────────────────────────────────────────

:done
if not defined NOPAUSE (
    echo.
    pause
)
endlocal
