@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
if errorlevel 1 goto directory_error
set "PYTHONUTF8=1"
set "VIRTUAL_ENV=%CD%\.venv"
set "PATH=%VIRTUAL_ENV%\Scripts;%PATH%"

if not exist ".venv\Scripts\python.exe" goto missing_venv
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if errorlevel 1 goto invalid_venv

".venv\Scripts\python.exe" "web_app.py" %*
set "result=%errorlevel%"
goto finish

:missing_venv
echo 未找到项目虚拟环境：.venv\Scripts\python.exe
set "result=1"
goto finish

:invalid_venv
echo 项目 .venv 环境无法运行，或 Python 版本低于 3.9。
set "result=1"
goto finish

:finish
echo.
if not "%result%"=="0" echo 程序未正常完成，请查看上方提示。
popd
pause
exit /b %result%

:directory_error
echo 无法进入项目目录。
pause
exit /b 1
