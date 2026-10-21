@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

REM ==========================================================================
REM  Loom GUI 启动器
REM
REM  ★ 这个文件是 UTF-8 源，构建后生成 GBK 的 loom-gui.bat。
REM    改了它之后必须跑：python dev/build_bat.py
REM    （原因见 dev/build_bat.py 顶部说明 —— cmd 按 GBK 逐字节读 bat）
REM
REM  ★ 本脚本刻意不用 enabledelayedexpansion、也不用 set /p：
REM    · 前者会让 echo 里落单的 `!` 吞掉半行文字
REM    · 后者分不清「用户敲回车」和「stdin 已关闭」
REM    这两个坑在 loom.bat 上都踩过，这里直接绕开。
REM ==========================================================================

echo.
echo ==============================================================
echo   Loom GUI  ^|  前端界面
echo ==============================================================
echo.

REM ---- 1. 检查 Node.js ---------------------------------------------------
where node >nul 2>&1
if errorlevel 1 (
    echo   [X] 没找到 node
    echo.
    echo       请先安装 Node.js 18 或更高版本：
    echo         https://nodejs.org/
    echo       装完关掉这个窗口，重新双击本脚本。
    goto done
)

for /f "tokens=*" %%v in ('node -v') do set "NODEVER=%%v"
echo   Node  %NODEVER%

REM ---- 2. 检查前端目录 ---------------------------------------------------
if not exist "frontend\package.json" (
    echo.
    echo   [X] 找不到 frontend\package.json
    echo       请确认本脚本放在 loom 目录下（与 frontend 同级）。
    goto done
)

REM ---- 3. 首次运行：装依赖 -----------------------------------------------
if not exist "frontend\node_modules" (
    echo.
    echo   首次运行，正在安装前端依赖...
    echo   （几分钟取决于网络，装完会自动继续）
    echo.
    pushd frontend
    call npm install
    set "INSTALL_RC=%errorlevel%"
    popd
    if not "%INSTALL_RC%"=="0" (
        echo.
        echo   [X] 依赖安装失败（exit=%INSTALL_RC%）
        echo.
        echo       常见原因：
        echo         1. 网络不通 —— 先确认能访问 npm 源
        echo         2. 代理拦截 —— 关掉系统代理再试
        echo       也可以手动执行：cd frontend ^&^& npm install
        goto done
    )
    echo.
    echo   依赖安装完成。
)

REM ---- 4. 端口检查（5173 被占时 vite 会直接报错，先给个提示）-----------
REM  ★ 必须再筛一次 LISTENING：只匹配 ":5173" 会把 TIME_WAIT 的残留连接
REM    也算进来 —— 服务关了但端口还在 TIME_WAIT 时，会被误判成"已被占用"。
set "PORT_BUSY=0"
netstat -ano | findstr ":5173 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 set "PORT_BUSY=1"

if "%PORT_BUSY%"=="1" (
    echo.
    echo   [提示] 5173 端口已被占用。
    echo          多半是上次的窗口还开着 —— 那就直接访问：
    echo.
    echo            http://localhost:5173
    echo.
    echo          如果不是，请先关掉占用 5173 的程序，再重跑本脚本。
    echo.
    echo   （这里不自动开浏览器：用 start 打开的话，浏览器会接管本窗口的
    echo     输出句柄，脚本调用方会一直等到浏览器退出。手动复制更稳。）
    goto done
)

REM ---- 5. 启动 -----------------------------------------------------------
echo.
echo   启动中，就绪后浏览器会自动打开
echo     地址  http://localhost:5173
echo     停止  Ctrl + C
echo     视图  http://localhost:5173/?view=execution   （直接看执行流）
echo.
echo --------------------------------------------------------------
echo.

pushd frontend
call npm run dev -- --open
popd

echo.
echo --------------------------------------------------------------
echo   前端服务已停止。

:done
echo.
echo --------------------------------------------------------------
echo   Loom GUI 退出。
echo --------------------------------------------------------------
echo.
pause
