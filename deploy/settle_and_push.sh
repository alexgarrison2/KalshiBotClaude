#!/bin/bash
PYTHON=/Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python
PROJECT=/Users/alexgarrison/KalshiBotClaude
cd $PROJECT

YESTERDAY=$(date -v-1d +%Y-%m-%d)
TODAY=$(date +%Y-%m-%d)

echo "=== Settlement run $(date) ===" >> logs/settlement.log
$PYTHON deploy/check_results.py --date $YESTERDAY >> logs/settlement.log 2>&1
$PYTHON deploy/check_results.py --date $TODAY      >> logs/settlement.log 2>&1

git add data/trades.csv
git diff --cached --quiet || (git commit -m "auto: daily settlement $TODAY" && git push)
