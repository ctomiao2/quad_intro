@echo off
rem 注册实盘计划任务：交易日 09:31 开盘任务 / 15:35 收盘任务（周一~周五，非交易日脚本自行跳过）
rem 需以当前用户运行；如需删除见 remove_schedule.bat
chcp 65001 >nul
setlocal
for /f "usebackq delims=" %%i in (`python -c "import sys; print(sys.executable)"`) do set PY=%%i
set SCRIPT=%~dp0..\live.py

schtasks /Create /TN "quad_intro_live_open"  /TR "\"%PY%\" \"%SCRIPT%\" open"  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:31 /F
schtasks /Create /TN "quad_intro_live_close" /TR "\"%PY%\" \"%SCRIPT%\" close" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:35 /F

echo.
echo 已注册: quad_intro_live_open (09:31) / quad_intro_live_close (15:35)
pause
