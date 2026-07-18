#!/bin/bash
# vault-rag incremental 재인덱싱 — n8n(맥미니, SSH 노드)이 웹훅마다 호출하는 단일 진입점.
#
# git pull로 vault 갱신 → 이전 HEAD와 diff해서 바뀐 .md만 embed.py --file/--delete.
# 전체 재인덱싱(--all) 안 씀 — 바뀐 파일만.
#
# 겹치는 웹훅(연속 push) 대비 — 전체를 파일락으로 감싸 직렬화:
#   동시 실행 시 git pull이 .git/index.lock 충돌로 죽거나(에러), DB DELETE가
#   행 잠금으로 기다리다 "더 늦게 commit된 쪽이 이김"(최신 내용이 아니라)이라
#   최신 push가 있었는데도 옛 버전이 남을 수 있음. 락으로 항상 한 번에 하나만
#   돌게 하면 두 번째는 대기 후 자기 HEAD 기준으로 다시 diff하므로 안전.
#   macOS엔 flock 명령이 없어 mkdir 원자성으로 구현.
#
# 사용: ~/homeserver/vault-rag/scripts/reindex_changed.sh
# 환경변수로 재정의 가능: VAULT_DIR, GIT_BIN
set -euo pipefail

VAULT_DIR="${VAULT_DIR:-$HOME/second-brain}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO_DIR/.venv/bin/python"
EMBED="$REPO_DIR/src/embed.py"
GIT="${GIT_BIN:-/usr/bin/git}"

LOCK_DIR="/tmp/vault-reindex.lock"
STALE_SEC=600     # 이보다 오래된 락은 죽은 걸로 보고 강제 회수
WAIT_MAX_SEC=300  # 이만큼 기다려도 못 잡으면 포기(에러 종료, n8n 실행이력에 남음)

acquire_lock() {
    local waited=0
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        local age
        age=$(( $(date +%s) - $(stat -f %m "$LOCK_DIR" 2>/dev/null || echo "$(date +%s)") ))
        if [ "$age" -gt "$STALE_SEC" ]; then
            echo "[reindex] stale 락(${age}s) 강제 회수"
            rm -rf "$LOCK_DIR"
            continue
        fi
        if [ "$waited" -ge "$WAIT_MAX_SEC" ]; then
            echo "[reindex] 락 대기 ${WAIT_MAX_SEC}s 초과, 포기"
            exit 1
        fi
        echo "[reindex] 다른 재인덱싱 진행 중, 대기... (${waited}s)"
        sleep 3
        waited=$((waited + 3))
    done
}
release_lock() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap release_lock EXIT

acquire_lock

cd "$VAULT_DIR"

BEFORE="$("$GIT" rev-parse HEAD)"
"$GIT" pull --quiet
AFTER="$("$GIT" rev-parse HEAD)"

if [ "$BEFORE" = "$AFTER" ]; then
    echo "[reindex] 변경 없음 ($BEFORE)"
    exit 0
fi

echo "[reindex] $BEFORE -> $AFTER"

"$GIT" diff --name-status "$BEFORE" "$AFTER" -- '*.md' | while IFS=$'\t' read -r status f1 f2; do
    case "$status" in
        A|M)
            echo "[reindex] 추가/수정: $f1"
            "$PY" "$EMBED" --file "$f1"
            ;;
        D)
            echo "[reindex] 삭제: $f1"
            "$PY" "$EMBED" --delete "$f1"
            ;;
        R*)
            echo "[reindex] 리네임: $f1 -> $f2"
            "$PY" "$EMBED" --delete "$f1"
            "$PY" "$EMBED" --file "$f2"
            ;;
        *)
            echo "[reindex] 알 수 없는 상태 '$status': $f1 (건너뜀)"
            ;;
    esac
done

echo "[reindex] 완료"
