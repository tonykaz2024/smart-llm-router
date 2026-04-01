@echo off
echo ============================================
echo  Smart LLM Router v2.0
echo  Starting on port 4001...
echo ============================================
echo.
echo  Prerequisites:
echo    - CCS CLIProxy running on :8317
echo    - DOT PC LiteLLM running on 192.168.10.38:4000
echo.
python F:\llm-router\smart-router.py --port 4001 %*
