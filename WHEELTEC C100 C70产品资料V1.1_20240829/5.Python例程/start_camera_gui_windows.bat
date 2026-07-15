@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    set "PYTHON=python"
)

%PYTHON% -c "import cv2; import PIL; import cv2_enumerate_cameras; from importlib.metadata import version; v=tuple(int(x) for x in version('cv2-enumerate-cameras').split('.')[:3]); assert v >= (1,3,3)" >nul 2>nul
if not %errorlevel%==0 (
    echo Installing or updating required Python packages...
    %PYTHON% -m pip install -r requirements.txt
    if not %errorlevel%==0 (
        echo Dependency installation failed.
        pause
        exit /b 1
    )
)

%PYTHON% camera_gui.py
if not %errorlevel%==0 pause
