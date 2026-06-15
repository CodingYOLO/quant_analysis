#!/bin/bash
# 盘前快讯 — 9:00 自动运行
PROJECT_DIR="/Users/vivianjin/Documents/quant/quant_stock_analysis/astock-agent"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_FILE="$PROJECT_DIR/logs/quick_$(date +%Y%m%d).log"
mkdir -p "$PROJECT_DIR/logs"
cd "$PROJECT_DIR" || exit 1
echo "===== $(date '+%H:%M:%S') 盘前快讯 =====" >> "$LOG_FILE"
"$PYTHON" -m app.run pre >> "$LOG_FILE" 2>&1
