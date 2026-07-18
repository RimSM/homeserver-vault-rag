#!/bin/bash
# vault-rag incremental 재인덱싱 — n8n(맥미니, SSH 노드)이 주기 호출하는 단일 진입점.
#
# git pull로 vault 갱신 → 이전 HEAD와 diff해서 바뀐 .md만 embed.py --file/--delete.
# 전체 재인덱싱(--all) 안 씀 — 바뀐 파일만.
#
# 사용: ~/homeserver/vault-rag/scripts/reindex_changed.sh
# 환경변수로 재정의 가능: VAULT_DIR, GIT_BIN
set -euo pipefail

VAULT_DIR="${VAULT_DIR:-$HOME/second-brain}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO_DIR/.venv/bin/python"
EMBED="$REPO_DIR/src/embed.py"
GIT="${GIT_BIN:-/usr/bin/git}"

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
