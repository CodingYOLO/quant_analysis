#!/bin/bash
# A股每日选股 — 自动运行脚本
# crontab 调用此脚本，避免环境变量问题

PROJECT_DIR="/Users/vivianjin/Documents/quant/quant_stock_analysis/astock-agent"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/run_$(date +%Y%m%d).log"

# 确保日志目录存在
mkdir -p "$LOG_DIR"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') 开始运行 =====" >> "$LOG_FILE"

cd "$PROJECT_DIR" || exit 1

# 运行流水线（推送到微信）
"$PYTHON" -m app.run run >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

echo "===== $(date '+%Y-%m-%d %H:%M:%S') 运行完成，退出码=$EXIT_CODE =====" >> "$LOG_FILE"

# 日志保留30天
find "$LOG_DIR" -name "run_*.log" -mtime +30 -delete

exit $EXIT_CODE
