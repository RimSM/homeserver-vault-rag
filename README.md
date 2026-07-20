# homeserver-vault-rag

옵시디언 vault(second brain)를 임베딩해 시맨틱 검색하는 개인 RAG.
프레임워크 없이 **순수 파이썬 hand-roll** (학습 목적).

- **저장**: 공용 infra의 Postgres(pgvector), DB `vault_rag` / schema `second_brain`
- **임베딩**: 공용 infra의 Ollama `bge-m3` (1024차원, 한국어 대응)
- **인프라**: [homeserver-infra](https://github.com/RimSM/homeserver-infra)
- 설계·결정 맥락은 코드가 아니라 vault: `01_Projects/99_RIMSM/004.vault-rag/overview.md`

## 파이프라인

```
[인덱싱] 노트 → 청킹(헤더 섹션) → bge-m3 임베딩 → Postgres(pgvector) 적재
                              + 위키링크 resolve → links 테이블
[질의]  질문 → bge-m3 임베딩 → 벡터 씨앗(seed_k 청크→노트) → links 1-hop 확장(이웃 노트)
             → 노트 union(best-chunk 코사인 정렬 + 이웃 슬롯 예약) → 상위 노트 목록 반환
```

**생성(LLM) 단계는 없음.** RAG는 "어느 노트/섹션(source_path + heading_trail)"만 반환하고,
**답변 종합은 Claude Code**가 그 경로의 로컬 vault 원문을 읽어서 한다.
→ Claude API·로컬 LLM 불필요 (생성기가 이미 Claude Code). 상세 → vault overview.md.

## 실행 (실행=prod 맥미니 / 코드=stage 맥북)

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env    # PGPASSWORD 등 채우기 (.env 는 git 제외)

# 전체 재인덱싱 (테이블 리셋 → 배치 commit)
./.venv/bin/python src/embed.py --all

# 특정 파일만 증분 인덱싱
./.venv/bin/python src/embed.py --file "01_Projects/.../note.md"

# 검색 (사람용 / Claude용 JSON) — 기본: 노트 union + 이웃 슬롯 예약
./.venv/bin/python src/embed.py --query "질문"
./.venv/bin/python src/embed.py --query "질문" --json
# 파라미터 조절 (기본 seed_k=10 / max-neighbors=10 / max-notes=10 / neighbor-reserve=3)
./.venv/bin/python src/embed.py --query "질문" --max-notes 12 --neighbor-reserve 4
```

### 청킹 단독 (표준 라이브러리만, pip 없이)
```bash
python src/chunk.py                 # vault 통계 + 샘플
python src/chunk.py --json > chunks.json
```

## 다음
- [x] 임베딩 → Postgres(pgvector) 적재 (`src/embed.py`)
- [x] 질의 파이프라인 (벡터검색 + links 1-hop resolve + 노트 union/이웃예약)
- [x] bge-m3 전환 (한국어 검색 품질) + 배치 재인덱싱
- [ ] incremental 재인덱싱 자동화 (`git diff --name-only` 기반)
- [ ] n8n 트리거 (맥미니 git pull 감지 → 변경 노트만 재인덱싱)
- [ ] 검색 품질 튜닝 (청킹 단위, seed-k / max-neighbors / top-k)
