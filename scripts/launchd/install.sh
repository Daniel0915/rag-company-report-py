#!/usr/bin/env bash
# 주간 공시 수집 LaunchAgent 설치. --dry-run이면 채워진 plist를 출력만 한다.
set -euo pipefail

LABEL="com.daniel0915.rag-company-report.ingest"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TEMPLATE="$SCRIPT_DIR/$LABEL.plist.template"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
elif [[ $# -gt 0 ]]; then
  echo "usage: install.sh [--dry-run]" >&2
  exit 2
fi

# 경로에 '|', '&', '\' 가 있으면 sed 치환이 깨지므로 이스케이프한다.
ESCAPED="$(printf '%s' "$PROJECT" | sed -e 's/[\\|&]/\\&/g')"
RENDERED="$(sed "s|__PROJECT__|$ESCAPED|g" "$TEMPLATE")"

if [[ "$DRY_RUN" == 1 ]]; then
  printf '%s\n' "$RENDERED"
  exit 0
fi

# launchd는 StandardOutPath 디렉터리를 만들지 않으므로 미리 만든다.
mkdir -p "$PROJECT/logs/ingest" "$HOME/Library/LaunchAgents"
printf '%s\n' "$RENDERED" > "$DEST"
plutil -lint "$DEST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "설치 완료: $DEST (매주 토요일 07:10 실행)"
