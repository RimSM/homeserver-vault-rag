"""
vault-rag HTTP 엔드포인트 — 훅이 SSH 대신 curl 로 부르는 검색 서버.

설계(→ vault 01_Projects/99_RIMSM/004.vault-rag/overview.md 2026-07-20 결정):
  회사망이 TLS 인스펙션(MITM)으로 HTTPS를 자기 CA로 재서명 → Tailscale 은 컨트롤
  서버 인증서를 엄격 검증(LE 루트 핀)하므로 연결 거부 = 회사망에서 구조적 사용 불가.
  반면 평범한 공개 HTTPS(rag.rimsm.com)는 회사 CA를 시스템이 신뢰하니 그냥 통과.
  → **RAG 질의 경로를 HTTP 하나로 통일**(맥·윈도우 공통). Tailscale/SSH 는 RAG 용에서
    빠지고 맥미니 관리·배포·디버깅용으로만 존속.

  cloudflared(Cloudflare Tunnel) → 이 서버 → embed.search() → JSON.
  CLI(`embed.py --query ... --json`)와 **완전히 같은 JSON**을 돌려준다(훅만 갈아끼우면 됨).

프레임워크 안 씀: 이 repo 기조대로 표준 라이브러리만(chunk.py 표준lib, load_env 도
  python-dotenv 미의존). 엔드포인트 1개짜리에 FastAPI+uvicorn 을 들일 이유 없음.

보안 (엔드포인트가 private vault 내용을 돌려주므로 뚫리면 second brain 유출):
  - **바인드는 127.0.0.1 고정.** cloudflared 가 같은 기기에서 붙으므로 LAN 에 열 이유 없음.
    0.0.0.0 으로 열면 Cloudflare Access(①겹)를 우회해 집 LAN 에서 맨몸으로 접근 가능해짐.
  - ① Cloudflare Access 서비스 토큰은 CF 엣지가 검사(여기 도달 전 403) → 서버는 관여 안 함.
  - ② 이 서버는 `X-RAG-Secret` 헤더를 확인(카드가 뚫려도 한 겹 더). RAG_SECRET 미설정이면
    **기동 거부**(fail-closed) — 실수로 무인증 공개되는 사고 방지.
  - 시크릿은 .env 로만 받고 로그에 절대 안 찍음. 질의는 앞 80자만 로깅.

사용:
    ./.venv/bin/python src/serve.py                  # 127.0.0.1:8787
    ./.venv/bin/python src/serve.py --port 9000

    # GET — 한글은 반드시 percent-encoding (--data-urlencode). 안 하면 400.
    curl --max-time 15 --get "http://127.0.0.1:8787/query" \
      --data-urlencode "q=테스트" -H "X-RAG-Secret: <RAG_SECRET>"

    # POST — 질의가 body 로 가서 인코딩 함정 없음(훅 권장 경로)
    curl --max-time 15 "http://127.0.0.1:8787/query" \
      -H "X-RAG-Secret: <RAG_SECRET>" -H "Content-Type: application/json" \
      -d '{"q":"테스트"}'
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import embed   # load_env/.env 파싱이 import 시점에 끝남 (embed.py 모듈 최상단)

# 검색 파라미터 기본값 — embed.py 의 argparse 기본값과 일치시킬 것.
DEFAULTS = {"seed_k": 10, "max_neighbors": 10, "max_notes": 10, "neighbor_reserve": 3}
MAX_Q_LEN = 1000        # 질문 길이 상한 (임베딩 비용 폭주·장난 요청 차단)
PARAM_CAP = 50          # 정수 파라미터 상한 (max_notes=99999 같은 요청 방어)

RAG_SECRET = os.environ.get("RAG_SECRET", "")


def _check_secret(handler: BaseHTTPRequestHandler) -> bool:
    """X-RAG-Secret 헤더 검증. 타이밍 공격 방지로 compare_digest."""
    given = handler.headers.get("X-RAG-Secret", "")
    return hmac.compare_digest(given, RAG_SECRET)


def _clamp_int(raw: str | None, default: int) -> int:
    """쿼리스트링 정수 파싱 — 이상한 값이면 조용히 기본값(요청 거절보단 관대하게)."""
    try:
        return max(0, min(PARAM_CAP, int(raw)))
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "vault-rag/1.0"

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        # /health — 인증 없이 살아있음만. vault 정보는 일절 안 실음(터널·기동 확인용).
        if parsed.path == "/health":
            self._send(200, {"ok": True})
            return

        if parsed.path != "/query":
            self._send(404, {"error": "not found", "hint": "GET /query?q=... | POST /query {\"q\":...}"})
            return

        qs = parse_qs(parsed.query)
        raw = {k: (qs.get(k) or [None])[0] for k in ("q", *DEFAULTS)}
        self._handle_query(raw)

    def do_POST(self):
        """POST /query — body: {"q": "...", "max_notes": 10, ...}

        GET 은 한글 질의를 반드시 percent-encoding 해야 한다(curl --data-urlencode).
        생짜 UTF-8 을 요청 라인에 넣으면 http.server 가 핸들러 도달 전에 400 을 뱉는데
        메시지가 "Bad request syntax" 라 원인을 알아보기 어렵다. POST 는 질의가 body 로
        가므로 그 함정이 구조적으로 없음 → 훅에서 인코딩 실수해도 안전한 경로.
        """
        if urlparse(self.path).path != "/query":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            self._send(400, {"error": "body 없음 또는 과대"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("object 아님")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            self._send(400, {"error": f"JSON 파싱 실패: {e}"})
            return
        self._handle_query({k: payload.get(k) for k in ("q", *DEFAULTS)})

    def _handle_query(self, raw: dict):
        """GET/POST 공통 — 인증 → 검증 → 검색. raw 는 q + 파라미터의 원시값 dict."""
        if not _check_secret(self):
            # 왜 틀렸는지(헤더 없음/값 불일치) 구분해 알려주지 않음 — 탐색 힌트 차단.
            self._send(401, {"error": "unauthorized"})
            return

        query = str(raw.get("q") or "").strip()
        if not query:
            self._send(400, {"error": "q(질문) 파라미터 필요"})
            return
        if len(query) > MAX_Q_LEN:
            self._send(400, {"error": f"q 가 너무 김 (>{MAX_Q_LEN}자)"})
            return

        params = {k: _clamp_int(raw.get(k), v) for k, v in DEFAULTS.items()}

        started = time.time()
        try:
            # 요청마다 새 연결 — 상시 서버라 커넥션을 오래 들고 있으면 DB 재기동·
            # 유휴 타임아웃에 죽은 소켓이 남는다. 개인용 QPS 라 연결비용(~수십ms)이 싸다.
            conn = embed.get_db_connection()
            try:
                results = embed.search(conn, query, **params)
            finally:
                conn.close()
        except Exception:
            # 내부 예외(DB 주소·Ollama 다운 등)는 클라에 안 흘림. 서버 로그로만.
            traceback.print_exc()
            self._send(503, {"error": "검색 실패 — 서버 로그 확인 (DB/Ollama 상태)"})
            return

        elapsed = time.time() - started
        self.log_message('query "%s" -> %d notes, %.2fs', query[:80], len(results), elapsed)
        self._send(200, {"query": query, "results": results})

    def log_message(self, fmt, *args):
        # 기본 구현은 stderr. 시크릿은 어떤 경로로도 안 찍힘(헤더 로깅 자체를 안 함).
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    ap = argparse.ArgumentParser(description="vault-rag HTTP 검색 엔드포인트")
    ap.add_argument("--host", default="127.0.0.1", help="바인드 주소 (기본 127.0.0.1 — 바꾸지 말 것)")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()

    if not RAG_SECRET:
        sys.exit("[!] RAG_SECRET 미설정. .env 에 긴 랜덤 문자열을 넣으세요.\n"
                 "    예: python3 -c \"import secrets;print(secrets.token_urlsafe(32))\"")

    # 기동 시 DB 한 번 찔러보고 죽을 거면 여기서 죽는다(터널 붙인 뒤 500 나는 것보다 나음).
    try:
        embed.get_db_connection().close()
    except Exception as e:
        sys.exit(f"[!] DB 연결 실패: {e}\n    .env(PGHOST 등)와 prod infra 상태 확인.")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[*] vault-rag serve → http://{args.host}:{args.port}/query  (Ctrl-C 종료)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 종료.")


if __name__ == "__main__":
    main()
