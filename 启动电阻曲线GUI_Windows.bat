@echo off
setlocal

cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    set "PYTHON=python"
)

%PYTHON% --version >nul 2>nul
if not %errorlevel%==0 (
    echo 未找到 Python。
    echo 请先安装 Python 3，并勾选 Add python.exe to PATH。
    echo 下载地址: https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

%PYTHON% -c "import serial" >nul 2>nul
if not %errorlevel%==0 (
    echo 正在安装串口依赖 pyserial...
    %PYTHON% -m pip install pyserial
    if not %errorlevel%==0 (
        echo.
        echo pyserial 安装失败。可以手动运行:
        echo %PYTHON% -m pip install pyserial
        echo.
        pause
        exit /b 1
    )
)

%PYTHON% ResistanceGUI_windows.py
