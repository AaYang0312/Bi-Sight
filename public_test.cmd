@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM ============================================================
REM  BI Agent 公网测试通道（敏感拓扑在本地配置，不进 Git）
REM
REM  用法:
REM    public_test.cmd          开启公网测试通道
REM    public_test.cmd close    关闭公网入口并清理隧道
REM ============================================================

set "BI_LOCAL_CONFIG=%~dp0public_test.local.cmd"
if not exist "%BI_LOCAL_CONFIG%" (
    echo [错误] 缺少本地配置 public_test.local.cmd
    echo 请复制 public_test.local.cmd.example 后填写实际地址。
    pause
    exit /b 1
)
call "%BI_LOCAL_CONFIG%"

if not defined BI_SSH_HOST goto :config_error
if not defined BI_PUBLIC_HOST goto :config_error
if not defined BI_AUTH_USER goto :config_error
if not defined BI_REMOTE_PORT goto :config_error
if not defined BI_LOCAL_PORT goto :config_error

if /i "%~1"=="close" goto :close

set "BI_NGINX_TEMPLATE=%~dp0deploy\nginx_bi_test.conf.template"
set "BI_NGINX_RENDERED=%TEMP%\bi_test_nginx_%RANDOM%_%RANDOM%.conf"
if not exist "%BI_NGINX_TEMPLATE%" (
    echo [错误] 缺少 nginx 模板: %BI_NGINX_TEMPLATE%
    pause
    exit /b 1
)

echo [1/4] 检查本地页面 127.0.0.1:%BI_LOCAL_PORT% ...
curl.exe -s -o nul --max-time 3 http://127.0.0.1:%BI_LOCAL_PORT%
if errorlevel 1 (
    echo   页面未启动，最小化窗口启动中 ...
    start "BI-Streamlit" /min cmd.exe /d /c "cd /d %~dp0 && uv run --env-file .env.app streamlit run app.py --server.address 127.0.0.1 --server.port %BI_LOCAL_PORT% --server.headless true"
    %SystemRoot%\System32\ping.exe -n 9 127.0.0.1 >nul
    curl.exe -s -o nul --max-time 3 http://127.0.0.1:%BI_LOCAL_PORT%
    if errorlevel 1 (
        echo   [错误] 页面启动失败，请手动运行检查：
        echo     uv run --env-file .env.app streamlit run app.py
        pause
        exit /b 1
    )
)
echo   页面正常

echo [2/4] 清理旧隧道、释放服务器端口并推送 nginx 入口 ...
%SystemRoot%\System32\taskkill.exe /F /FI "WINDOWTITLE eq BI-Tunnel*" >nul 2>&1
%SystemRoot%\System32\ping.exe -n 2 127.0.0.1 >nul
ssh -o BatchMode=yes -o ConnectTimeout=10 %BI_SSH_HOST% "sudo fuser -k %BI_REMOTE_PORT%/tcp 2>/dev/null || true; sleep 1; ! ss -tln | grep -q ':%BI_REMOTE_PORT% '"
if errorlevel 1 (
    echo   [错误] 无法释放服务器隧道端口
    pause
    exit /b 1
)

%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -NonInteractive -Command "$t=[IO.File]::ReadAllText($env:BI_NGINX_TEMPLATE); $t=$t.Replace('__BI_PUBLIC_HOST__',$env:BI_PUBLIC_HOST).Replace('__BI_REMOTE_PORT__',$env:BI_REMOTE_PORT); [IO.File]::WriteAllText($env:BI_NGINX_RENDERED,$t,(New-Object Text.UTF8Encoding($false)))"
if errorlevel 1 (
    echo   [错误] nginx 模板渲染失败
    pause
    exit /b 1
)

ssh -o BatchMode=yes -o ConnectTimeout=10 %BI_SSH_HOST% "sudo tee /etc/nginx/conf.d/bi_test.conf >/dev/null" < "%BI_NGINX_RENDERED%"
set "BI_PUSH_RC=%ERRORLEVEL%"
del /q "%BI_NGINX_RENDERED%" >nul 2>&1
if not "%BI_PUSH_RC%"=="0" (
    echo   [错误] nginx 配置推送失败
    pause
    exit /b 1
)
ssh -o BatchMode=yes -o ConnectTimeout=10 %BI_SSH_HOST% "sudo nginx -t 2>&1 && sudo systemctl reload nginx"
if errorlevel 1 (
    echo   [错误] nginx 重载失败
    pause
    exit /b 1
)
echo   nginx 已就绪

echo [3/4] 建立 SSH 反向隧道（最小化窗口，关闭该窗口即断开隧道）...
start "BI-Tunnel" /min cmd.exe /d /c "ssh -N -R 127.0.0.1:%BI_REMOTE_PORT%:127.0.0.1:%BI_LOCAL_PORT% -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes %BI_SSH_HOST%"
%SystemRoot%\System32\ping.exe -n 5 127.0.0.1 >nul

echo [4/4] 验证隧道 ...
set "BI_CODE_FILE=%TEMP%\bi_test_code_%RANDOM%_%RANDOM%.txt"
set "CODE="
ssh -o BatchMode=yes -o ConnectTimeout=10 %BI_SSH_HOST% "curl -s -o /dev/null -w %%{http_code} --max-time 8 http://127.0.0.1:%BI_REMOTE_PORT%" > "%BI_CODE_FILE%"
set "BI_VERIFY_RC=%ERRORLEVEL%"
set /p CODE=<"%BI_CODE_FILE%"
del /q "%BI_CODE_FILE%" >nul 2>&1
if not "%BI_VERIFY_RC%"=="0" (
    echo   [错误] SSH 隧道验证命令失败
    pause
    exit /b 1
)
if "%CODE%"=="200" (
    echo   隧道正常
) else (
    echo   [错误] 隧道未通（服务器侧返回 %CODE%）
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   公网测试地址: https://%BI_PUBLIC_HOST%/
echo   用户名: %BI_AUTH_USER%    口令: 服务器上既有 htpasswd 口令
echo   自签证书提示不安全时，选择 高级 - 继续访问
echo.
echo   测试完成后运行: public_test.cmd close
echo ============================================================
pause
exit /b 0

:close
echo 关闭服务器公网入口与残留隧道 ...
ssh -o BatchMode=yes -o ConnectTimeout=10 %BI_SSH_HOST% "sudo fuser -k %BI_REMOTE_PORT%/tcp 2>/dev/null || true; sudo rm -f /etc/nginx/conf.d/bi_test.conf && sudo nginx -t >/dev/null && sudo systemctl reload nginx"
set "BI_CLOSE_RC=%ERRORLEVEL%"
%SystemRoot%\System32\taskkill.exe /F /FI "WINDOWTITLE eq BI-Tunnel*" >nul 2>&1
if not "%BI_CLOSE_RC%"=="0" (
    echo [错误] 服务器公网入口关闭失败，请人工检查。
    pause
    exit /b 1
)
echo 公网入口、服务器端口及本地隧道已关闭。
pause
exit /b 0

:config_error
echo [错误] public_test.local.cmd 缺少必要配置项。
echo 请参考 public_test.local.cmd.example。
pause
exit /b 1
