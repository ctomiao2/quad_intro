@echo off
rem 删除实盘计划任务
chcp 65001 >nul
schtasks /Delete /TN "quad_intro_live_open" /F
schtasks /Delete /TN "quad_intro_live_close" /F
echo 已删除实盘计划任务
pause
