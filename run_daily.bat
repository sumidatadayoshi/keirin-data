@echo off
REM 前日分のKEIRINデータを取得するバッチ (タスクスケジューラから起動する用)
cd /d "%~dp0"
python keirin_scraper.py --when yesterday --interval 1.5 --db data\keirin_2026q4.db >> data\run_daily.log 2>&1
