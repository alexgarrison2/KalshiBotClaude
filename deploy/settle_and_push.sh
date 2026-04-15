#!/bin/bash
PYTHON=/Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python
PROJECT=/Users/alexgarrison/KalshiBotClaude
cd $PROJECT

echo "=== Settlement run $(date) ===" >> logs/settlement.log

# Check last 4 placement dates — trades placed up to 3 days ago can be for yesterday's events
for i in 3 2 1 0; do
  DATE=$(date -v-${i}d +%Y-%m-%d)
  $PYTHON deploy/check_results.py --date $DATE >> logs/settlement.log 2>&1
done

git add data/trades.csv
git diff --cached --quiet || (git commit -m "auto: daily settlement $(date +%Y-%m-%d)" && git push)
