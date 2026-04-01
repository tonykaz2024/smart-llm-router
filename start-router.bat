@echo off
echo ============================================
echo  Smart LLM Router v2.5
echo  Starting on port 4001...
echo ============================================
echo.
set PYTHONUTF8=1
if exist "%~dp0.env" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%~dp0.env") do (
        if not "%%a"=="" if not "%%a:~0,1%"=="#" set "%%a=%%b"
    )
)
python "%~dp0smart-router.py" --port 4001 %*
