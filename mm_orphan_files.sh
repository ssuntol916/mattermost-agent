#!/bin/bash
# Mattermost: DB에서 삭제 표시(deleteat>0)됐지만 볼륨에 물리 파일이 남은 orphan 탐지
# 사용법:
#   ./mm_orphan_files.sh          # 목록만 확인
#   ./mm_orphan_files.sh --purge  # 남은 물리 파일(+preview/thumb 폴더) 실제 삭제
set -euo pipefail

PG=mattermost-postgres
VOL=mattermost_mattermost-data
PURGE="${1:-}"

# DB에서 삭제 표시된 원본 path 추출 → 볼륨과 대조
docker exec "$PG" psql -U mmuser -d mattermost -t -A \
  -c "SELECT path FROM fileinfo WHERE deleteat > 0;" | \
docker run --rm -i -v "$VOL":/data alpine sh -c '
  purge="'"$PURGE"'"
  n=0; orphan=0
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    n=$((n+1))
    if [ -f "/data/$p" ]; then
      sz=$(stat -c %s "/data/$p")
      orphan=$((orphan+1))
      if [ "$purge" = "--purge" ]; then
        rm -rf "$(dirname "/data/$p")"   # 원본+preview+thumb 폴더째 삭제
        echo "[삭제완료 ${sz}B] $p"
      else
        echo "[남아있음 ${sz}B] $p"
      fi
    fi
  done
  echo "----"
  if [ "$purge" = "--purge" ]; then
    echo "DB삭제 표시 $n건 중 물리 잔존 $orphan건 → 모두 물리 삭제함"
  else
    echo "DB삭제 표시 $n건 중 물리 잔존(orphan) $orphan건 (삭제하려면 --purge)"
  fi
'
