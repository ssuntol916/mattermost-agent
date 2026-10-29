#!/bin/zsh
# bridge.log 주간 로테이션
# - 크기가 THRESHOLD 초과 시에만 아카이브(gzip) 후 in-place truncate
# - copytruncate 방식: 실행 중인 브리지 데몬 재시작 불필요 (fd 유지)
# - 최근 KEEP 개 아카이브만 남기고 삭제
# cron: 매주 월요일 04:00 실행

set -e

LOG="${BRIDGE_LOG:-${0:A:h}/bridge.log}"   # 기본: 스크립트 옆 bridge.log
THRESHOLD=$((5 * 1024 * 1024))   # 5MB
KEEP=4

[ -f "$LOG" ] || exit 0

SIZE=$(stat -f %z "$LOG")
if [ "$SIZE" -lt "$THRESHOLD" ]; then
    echo "$(date '+%Y-%m-%d %H:%M') skip: ${SIZE}B < ${THRESHOLD}B"
    exit 0
fi

STAMP=$(date +%Y%m%d)
gzip -c "$LOG" > "${LOG}.${STAMP}.gz"
: > "$LOG"
echo "$(date '+%Y-%m-%d %H:%M') rotated: ${SIZE}B -> ${LOG}.${STAMP}.gz"

# 오래된 아카이브 정리 (최신 KEEP개 유지)
ls -1t "${LOG}".*.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f "$old"
    echo "$(date '+%Y-%m-%d %H:%M') removed old archive: $old"
done
