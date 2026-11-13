@echo off
"%USERPROFILE%\pc-panel\.venv-windows\Scripts\python.exe" "%USERPROFILE%\pc-panel\host\rmine_control.py" %*
exit /b %errorlevel%
