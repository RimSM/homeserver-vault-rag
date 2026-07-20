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

## HTTP 엔드포인트 (`src/serve.py`)

훅이 SSH 대신 `curl` 로 부르는 검색 서버. **회사망은 TLS 검열로 Tailscale 이 구조적으로
막히지만** 평범한 공개 HTTPS 는 통과 → RAG 질의 경로를 HTTP 하나로 통일.
(Tailscale/SSH 는 RAG 용에서 빠지고 맥미니 관리·배포용으로만 존속. 상세 → vault overview.md)

추가 의존성 없음 — 표준 라이브러리 `http.server`. **상시 구동은 홈서버 전체 원칙(서비스=docker
compose)과 맞춰 컨테이너로.** (다른 모든 서비스와 격리 방식 통일 — 맨몸 launchd 프로세스 아님.)

```bash
# .env 에 RAG_SECRET 채운 뒤 (미설정이면 기동 거부 = fail-closed)

# 컨테이너로 상시 구동 (권장 — home-server-network 필요: docker network create home-server-network)
docker compose -f docker-compose.vault-rag.yml up -d --build
# host 쪽도 127.0.0.1만 publish(docker-compose.vault-rag.yml) — LAN 노출 0, 기존 바인드 요구사항 그대로 유지.
# .env는 컨테이너에 통째로 bind mount(embed.py가 os.environ이 아니라 .env 파일을 직접 읽으므로).

# 로컬 디버깅용 (컨테이너 없이 직접)
./.venv/bin/python src/serve.py            # 127.0.0.1:8787 고정 바인드

# POST — 질의가 body 라 인코딩 함정 없음 (훅 권장 경로)
curl --max-time 15 "https://rag.rimsm.com/query" \
  -H "CF-Access-Client-Id: <서비스토큰 id>" \
  -H "CF-Access-Client-Secret: <서비스토큰 secret>" \
  -H "X-RAG-Secret: <RAG_SECRET>" \
  -H "Content-Type: application/json" -d '{"q":"질문"}'

# GET — 한글은 반드시 percent-encoding (--data-urlencode). 안 하면 400.
curl --max-time 15 --get "https://rag.rimsm.com/query" \
  --data-urlencode "q=질문" -H "X-RAG-Secret: <RAG_SECRET>" # + CF 헤더
```

응답 JSON 은 `embed.py --query --json` 과 **완전히 동일**(훅만 갈아끼우면 됨).
`GET /health` 는 무인증 `{"ok":true}` — 터널·기동 확인용, vault 정보 일절 없음.

**보안 4겹**: ① Cloudflare Access 서비스 토큰(엣지에서 403) → ② `X-RAG-Secret` 헤더
→ ③ `127.0.0.1` 고정 바인드(집 LAN 에서도 맨몸 접근 불가) → ④ `RAG_SECRET` 없으면 기동 거부.

## 다음
- [x] 임베딩 → Postgres(pgvector) 적재 (`src/embed.py`)
- [x] 질의 파이프라인 (벡터검색 + links 1-hop resolve + 노트 union/이웃예약)
- [x] bge-m3 전환 (한국어 검색 품질) + 배치 재인덱싱
- [ ] incremental 재인덱싱 자동화 (`git diff --name-only` 기반)
- [ ] n8n 트리거 (맥미니 git pull 감지 → 변경 노트만 재인덱싱)
- [ ] 검색 품질 튜닝 (청킹 단위, seed-k / max-neighbors / top-k)
- [x] HTTP 엔드포인트 (`src/serve.py`) — SSH → curl 전환용
- [ ] Cloudflare Tunnel ingress `rag.rimsm.com` + Access 서비스 토큰 (대시보드 작업)
- [x] 맥미니 상시 구동 = docker compose (`Dockerfile` + `docker-compose.vault-rag.yml`, 2026-07-20 — 홈서버 전체 원칙과 통일, launchd 계획 폐기)
- [ ] 맥미니에서 실제 `docker compose up -d` 배포 (지금은 stage 맥북에서만 검증)
- [ ] 훅 2개(`.sh`/`.ps1`)를 curl 로 교체
