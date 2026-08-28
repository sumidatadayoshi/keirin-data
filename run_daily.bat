@echo off
REM 前日分のKEIRINデータを取得するバッチ (タスクスケジューラから起動する用)
cd /d "%~dp0"
python keirin_scraper.py --when yesterday --interval 1.5 >> data\run_daily.log 2>&1
