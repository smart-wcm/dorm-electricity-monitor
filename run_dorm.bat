@echo off
REM 定时任务启动器：无论仓库克隆到哪，都切到本脚本所在目录再运行
cd /d "%~dp0"
".venv\Scripts\python.exe" dorm_elec_auto.py
