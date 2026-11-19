#!/usr/bin/env bash
# 주간 공시 수집 LaunchAgent 제거.
set -euo pipefail

LABEL="com.daniel0915.rag-company-report.ingest"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$DEST"
echo "제거 완료: $LABEL"
