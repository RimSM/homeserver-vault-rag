# homeserver-vault-rag

옵시디언 vault(second brain)를 임베딩해 시맨틱 검색하는 개인 RAG.
프레임워크 없이 **순수 파이썬 hand-roll** (학습 목적).

- **저장**: 공용 infra의 Postgres(pgvector), DB `vault_rag` / schema `second_brain`
- **임베딩**: 공용 infra의 Ollama `bge-m3` (1024차원, 한국어 대응)
- **질의**: `https://rag.rimsm.com/query` (Cloudflare Tunnel + Access 서비스 토큰) — 어느 기기·어느 망에서든
- **인프라**: [homeserver-infra](https://github.com/RimSM/homeserver-infra) — Postgres/Ollama/cloudflared
- 설계·결정 맥락은 코드가 아니라 vault: `01_Projects/99_RIMSM/004.vault-rag/overview.md`

## 파이프라인

```
[인덱싱] 노트 → 청킹(헤더 섹션) → bge-m3 임베딩 → Postgres(pgvector) 적재
                              + 위키링크 resolve → links 테이블
         └ second-brain push → GitHub webhook → n8n → scripts/reindex_changed.sh (변경분만)

[질의]  Claude Code 훅("rag" 감지) → curl → rag.rimsm.com → serve.py
        → 질문 bge-m3 임베딩 → 벡터 씨앗(seed_k 청크→노트) → links 1-hop 확장(이웃 노트)
        → 노트 union(best-chunk 코사인 정렬 + 이웃 슬롯 예약) → 상위 노트 목록(JSON)
```

**생성(LLM) 단계는 없음.** RAG는 "어느 노트/섹션(source_path + heading_trail)"만 반환하고,
**답변 종합은 Claude Code**가 그 경로의 로컬 vault 원문을 읽어서 한다.
→ Claude API·로컬 LLM 불필요 (생성기가 이미 Claude Code). 상세 → vault overview.md.

응답 항목의 `via` 는 그 노트가 어떻게 뽑혔는지 — `vector`(질의와 직접 유사) / `graph-1hop`
(씨앗 노트가 위키링크로 가리키는 이웃). 이웃 슬롯을 예산 절반으로 클램프해 씨앗이 전멸하지 않게 함.

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

### 자동 재인덱싱 (`scripts/reindex_changed.sh`)
`second-brain` push → GitHub webhook → n8n → 맥미니에서 이 스크립트 실행.
`git pull` 후 이전 HEAD와 diff해서 **바뀐 `.md`만** `--file`/`--delete`(전체 재인덱싱 안 함).
연속 push로 겹쳐 돌면 옛 버전이 남을 수 있어 **파일락(mkdir 원자성)으로 직렬화**
— macOS엔 `flock` 이 없음. 상세 → vault `automation.md`.

## HTTP 엔드포인트 (`src/serve.py`)

훅이 SSH 대신 `curl` 로 부르는 검색 서버. **회사망은 TLS 검열로 Tailscale 이 구조적으로
막히지만** 평범한 공개 HTTPS 는 통과 → RAG 질의 경로를 HTTP 하나로 통일.
(Tailscale/SSH 는 RAG 용에서 빠지고 맥미니 관리·배포용으로만 존속. 상세 → vault overview.md)

`rag.rimsm.com` 은 **vault-rag 전용 Cloudflare 터널**(n8n 과 분리)로 들어온다. 터널 설정은
infra repo(`cloudflared-rag/config.yml`) — 터널은 CLI 에 `rename` 이 없어서, 서비스가 늘면
공유하지 말고 새로 파는 게 낫다.

추가 의존성 없음 — 표준 라이브러리 `http.server`. **상시 구동은 홈서버 전체 원칙(서비스=docker
compose)과 맞춰 컨테이너로.** (다른 모든 서비스와 격리 방식 통일 — 맨몸 launchd 프로세스 아님.)

```bash
# .env 에 RAG_SECRET 채운 뒤 (미설정이면 기동 거부 = fail-closed)

# 컨테이너로 상시 구동 (권장 — home-server-network 필요: docker network create home-server-network)
docker compose -f docker-compose.vault-rag.yml up -d --build
# host 쪽도 127.0.0.1만 publish — LAN 노출 0, 기존 바인드 요구사항 그대로 유지.
# .env는 /app/.env.host 로 bind mount(embed.py가 os.environ이 아니라 .env 파일을 직접 읽음).
#   → docker-entrypoint.sh 가 사본을 만들며 PGHOST/OLLAMA_URL 이 localhost/127.0.0.1 이면
#     host.docker.internal 로 치환. 컨테이너 안에서 localhost 는 컨테이너 자신이라 DB 를 못 찾음.

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

실측 검증: 인증 없음 → 401 / 틀린 서비스 토큰 → 403 / 정상 → 200.
⚠️ **"되는지" 말고 "안 되는 게 진짜 막히는지"로 검증할 것.** Access Application 을 만들 때
"하위 도메인" 칸에 풀도메인(`rag.rimsm.com`)을 넣으면 뒤에 도메인이 또 붙어
(`rag.rimsm.com.rimsm.com`) 실제 트래픽이 정책에 안 걸린다 — 에러 없이 조용히 무력화되므로
정상 요청만 테스트하면 못 잡는다. 서브도메인만(`rag`) 넣을 것.

### 클라이언트(훅) 설정
Claude Code 훅(`~/.claude/hooks/vault-rag-trigger.sh` / `.ps1`)이 이 엔드포인트를 부른다.
시크릿은 훅에 박지 않고(dotclaude repo 로 동기화됨) **이 repo 의 `.env` 경로를 하드코딩해서
읽는다** — 기기마다 경로가 다르므로 훅에서 직접 지정. 절차 → vault `remote-access.md`.
`RAG_SECRET` 은 **모든 기기가 같은 값**이어야 한다(다르면 401).

## 진행 상황

**Phase 1 — 코어 (완료)**
- [x] 임베딩 → Postgres(pgvector) 적재 (`src/embed.py`)
- [x] 질의 파이프라인 (벡터검색 + links 1-hop resolve + 노트 union/이웃예약)
- [x] bge-m3 전환 (한국어 검색 품질) + 배치 재인덱싱
- [x] incremental 재인덱싱 자동화 (`scripts/reindex_changed.sh`, git diff 기반 + 파일락)
- [x] n8n 트리거 (GitHub webhook → 변경 노트만 재인덱싱)

**Phase 2 — 어디서나 접근 (완료, 2026-07-20/21)**
- [x] HTTP 엔드포인트 (`src/serve.py`) — 표준 라이브러리만, 추가 의존성 0
- [x] 상시 구동 = docker compose (`Dockerfile` + `docker-compose.vault-rag.yml`) — launchd 계획 폐기, 홈서버 전체 원칙과 통일
- [x] 맥미니(prod) 배포 + 컨테이너 안 `localhost` 자기참조 수정
- [x] Cloudflare Tunnel `rag.rimsm.com` (vault-rag 전용 터널, n8n 과 분리)
- [x] Cloudflare Access 서비스 토큰 + 차단 실측 검증 (401/403/200)
- [x] 맥 훅(`vault-rag-trigger.sh`) SSH → curl 전환
- [ ] 회사 윈도우 훅(`vault-rag-trigger.ps1`) curl 전환 ← **남은 것**

**Phase 3 — 품질·확장 (예정)**
- [ ] 검색 품질 튜닝 (청킹 단위, seed-k / max-neighbors / top-k)
      — 흔한 단어("전략" 등)에 노이즈가 딸려옴. sim 하한 도입은 기각 방향 → vault `search-pipeline.md`
- [ ] 헤더-only 잡청크 처리 → vault `chunking.md`
- [ ] 월간 vault 건강검진 잡 (orphan 노트·깨진 링크·의미 중복 탐지, 제안만/자동수정 X)
