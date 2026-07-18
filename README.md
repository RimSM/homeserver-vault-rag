# homeserver-vault-rag

옵시디언 vault(second brain)를 임베딩해 시맨틱 검색/질의하는 개인 RAG.
프레임워크 없이 **순수 파이썬 hand-roll** (학습 목적).

- **저장**: 공용 infra의 Postgres(pgvector), DB `vault_rag` / schema `second_brain`
- **임베딩**: 공용 infra의 Ollama `nomic-embed-text` (768차원)
- **인프라**: [homeserver-infra](https://github.com/RimSM/homeserver-infra)
- 설계·결정 맥락은 코드가 아니라 vault: `01_Projects/99_RIMSM/004.vault-rag/overview.md`

## 파이프라인 (Phase 1 POC, 맥북 stage)

```
노트 → 청킹(헤더 섹션) → 임베딩(Ollama) → Postgres(pgvector) 적재
질의 → 임베딩 → 벡터 top-k → 그래프 1-hop(links) → 재정렬 → LLM(Claude)
```

### 청킹
```bash
python src/chunk.py                 # vault 통계 + 샘플 청크
python src/chunk.py --sample 5
python src/chunk.py --json > chunks.json
```
표준 라이브러리만 사용 → pip 설치 없이 실행.

## 다음
- [ ] 임베딩 → Postgres(pgvector) 적재 (`src/embed.py`)
- [ ] 질의 파이프라인 (벡터검색 + links 1-hop + rerank)
- [ ] incremental 재인덱싱 (`git diff --name-only`)
- [ ] n8n 트리거 (git pull 감지 → 재인덱싱)
