@echo off
REM ============================================================
REM  停止后台运行的宿舍电费监控服务。
REM  同时结束两种运行形态：源码模式(pythonw.exe) 与 打包模式(宿舍电费监控.exe)。
REM  用法：双击本文件 -> 终止进程并清理锁文件。
REM ============================================================
cd /d "%~dp0"
echo Stopping Dorm Electricity Monitor ...
taskkill /F /IM pythonw.exe 2>nul
taskkill /F /IM "宿舍电费监控.exe" 2>nul
if exist "%~dp0.dorm_elec.lock" del /Q "%~dp0.dorm_elec.lock"
echo Done. You may close this window.
