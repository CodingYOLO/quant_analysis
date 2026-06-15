#!/bin/bash
# 盘中半天快讯 — 12:00 自动运行
PROJECT_DIR="/Users/vivianjin/Documents/quant/quant_stock_analysis/astock-agent"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_FILE="$PROJECT_DIR/logs/quick_$(date +%Y%m%d).log"
mkdir -p "$PROJECT_DIR/logs"
cd "$PROJECT_DIR" || exit 1
echo "===== $(date '+%H:%M:%S') 盘中快讯 =====" >> "$LOG_FILE"
"$PYTHON" -m app.run mid >> "$LOG_FILE" 2>&1
