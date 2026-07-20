"""
vault-rag Phase 1 — 임베딩 · 적재 · 검색 (bge-m3 + pgvector)

설계(→ vault 01_Projects/99_RIMSM/004.vault-rag/overview.md):
  질의 → [이 서비스: bge-m3 임베딩 → 벡터 씨앗 → 링크 resolve → 1-hop 확장 → 노트 union]
       → "어느 노트(source_path + best-chunk heading_trail)"만 반환
  → 생성은 Claude Code(나)가 그 경로의 로컬 vault .md 를 Read 해서 종합. (LLM 생성 단계 없음)

핵심 리팩토링 (2026-07-18):
  - 임베딩 nomic-embed-text(768d) → bge-m3(1024d). 프리픽스 없음. Ollama /api/embed 배치.
  - links.target_link(위키링크 원문) → target_path 로 resolve 저장 (1-hop 조인 살림).
  - 진짜 1-hop 확장 + rerank: 벡터 top-k 노트의 링크 이웃 청크를 후보에 추가 후 쿼리 코사인 재정렬.
  - index_all: 배치(기본 100개) commit + 진행 카운트 체킹.

검색 리팩토링 (2026-07-20):
  - 청크 top-k 칼질 폐기 → **노트 단위 union 반환**. 옛 방식은 seed_k(10)≥top_k(5)면 이웃이
    씨앗을 쿼리-코사인으로 못 이겨 최종 진입 불가(증명된 no-op)였음. search() docstring 참고.
  - **이웃 슬롯 예약(neighbor_reserve)**: 이웃에게 최소 슬롯 보장(floor) → 1-hop이 실제로 살아남음.
  - 기본값: seed_k=10, max_neighbors=10, max_notes=10, neighbor_reserve=3. 노트 점수=best-chunk 코사인.

사용:
    python src/embed.py --all                     # 전체 재인덱싱 (테이블 리셋 후 배치 적재)
    python src/embed.py --file "path/to/note.md"  # 특정 파일 증분 인덱싱
    python src/embed.py --query "질문" --json      # 검색 (Claude가 먹기 좋은 JSON, 기본 노트 union)
    python src/embed.py --query "질문" --max-notes 10 --neighbor-reserve 3   # 상한/이웃예약 조절
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
import psycopg
from pgvector.psycopg import register_vector

from chunk import Chunk, chunk_note, build_chunks, iter_markdown_files


# ---------------------------------------------------------------- 환경 로드
def load_env(base_dir: Path):
    """.env 수동 파싱 (python-dotenv 미의존)."""
    env_path = base_dir / ".env"
    if not env_path.exists():
        sys.exit(f"[!] {env_path} 없음. .env.example 참고해 생성하세요.")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip("'\"")


BASE_DIR = Path(__file__).resolve().parent.parent
load_env(BASE_DIR)

VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/Users/rimsm/second-brain"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))   # bge-m3 = 1024
SCHEMA = "second_brain"
BATCH = int(os.environ.get("INDEX_BATCH", "100"))       # "100개 넣고 체킹"

PG = dict(
    host=os.environ.get("PGHOST", "localhost"),
    port=os.environ.get("PGPORT", "5432"),
    dbname=os.environ.get("PGDATABASE", "vault_rag"),
    user=os.environ.get("PGUSER", "vault_rag_app"),
    password=os.environ.get("PGPASSWORD", ""),
)


def get_db_connection():
    conn = psycopg.connect(" ".join(f"{k}={v}" for k, v in PG.items()), connect_timeout=10)
    register_vector(conn)   # 파이썬 list ↔ pgvector vector 자동 어댑팅
    return conn


# ---------------------------------------------------------------- 임베딩 (bge-m3)
def embed_batch(texts: list[str]) -> list[list[float]]:
    """Ollama /api/embed 배치 임베딩. bge-m3는 프리픽스 불필요."""
    if not texts:
        return []
    resp = requests.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": texts},
        timeout=300,
    )
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as he:
        raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:300]}") from he
    embs = resp.json().get("embeddings")
    if not embs or len(embs) != len(texts):
        raise RuntimeError(f"임베딩 응답 개수 불일치: got {len(embs) if embs else 0}, want {len(texts)}")
    return embs


def embed_one(text: str) -> list[float]:
    return embed_batch([text])[0]


# ---------------------------------------------------------------- 링크 resolve
class NoteIndex:
    """위키링크 원문 → 실제 vault 상대경로(source_path) 해석기.

    Obsidian 규칙 근사:
      - 경로형(`99_AI/RULES`)         → `99_AI/RULES.md`
      - 상대형(`../readme`)           → source 노트 폴더 기준으로 정규화
      - 맨 파일명(`file-formats`)     → 같은 stem 노트 (여럿이면 최단 경로)
    해석 실패는 None (외부/깨진 링크).
    """
    def __init__(self, vault: Path):
        self.paths: set[str] = set()
        self.by_stem: dict[str, list[str]] = {}
        for p in iter_markdown_files(vault):
            rel = str(p.relative_to(vault))
            self.paths.add(rel)
            self.by_stem.setdefault(p.stem, []).append(rel)
        for stem in self.by_stem:                     # 최단 경로 우선 (Obsidian shortest-path)
            self.by_stem[stem].sort(key=lambda s: (s.count("/"), len(s)))

    def resolve(self, source_path: str, target_link: str) -> str | None:
        t = target_link.strip().rstrip("/")
        if t.endswith(".md"):
            t = t[:-3]
        if not t:
            return None
        # 상대 경로
        if t.startswith("./") or t.startswith("../"):
            base = os.path.dirname(source_path)
            cand = os.path.normpath(os.path.join(base, t)) + ".md"
            return cand if cand in self.paths else None
        # 경로형 (슬래시 포함, vault 루트 기준)
        if "/" in t:
            cand = t + ".md"
            if cand in self.paths:
                return cand
            # 폴백: 맨 파일명으로도 시도
            t = t.rsplit("/", 1)[-1]
        # 맨 파일명 → stem 매칭
        hits = self.by_stem.get(t)
        return hits[0] if hits else None


# ---------------------------------------------------------------- DDL
def reset_tables(conn):
    """전체 재인덱싱용: 테이블 드롭 후 재생성 (차원 바뀌면 필수)."""
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA} AUTHORIZATION {PG['user']};")
        cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.chunks CASCADE;")
        cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.links CASCADE;")
        conn.commit()
    ensure_tables(conn)
    print(f"[*] 테이블 리셋 완료 (embedding vector({EMBED_DIM}), model={EMBED_MODEL}).")


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA} AUTHORIZATION {PG['user']};")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.chunks (
                id            SERIAL PRIMARY KEY,
                source_path   TEXT NOT NULL,
                title         TEXT NOT NULL,
                heading_trail TEXT NOT NULL,
                heading       TEXT NOT NULL,
                level         INT  NOT NULL,
                text          TEXT NOT NULL,
                embedding     vector({EMBED_DIM}) NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.links (
                id          SERIAL PRIMARY KEY,
                source_path TEXT NOT NULL,
                target_link TEXT NOT NULL,       -- 위키링크 원문
                target_path TEXT,                -- resolve된 실제 노트 경로 (없으면 NULL)
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_chunks_source ON {SCHEMA}.chunks(source_path);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_links_source ON {SCHEMA}.links(source_path);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_links_target ON {SCHEMA}.links(target_path);")
        conn.commit()


# ---------------------------------------------------------------- 적재
def insert_chunks(cur, rows: list[tuple]):
    cur.executemany(f"""
        INSERT INTO {SCHEMA}.chunks
            (source_path, title, heading_trail, heading, level, text, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s);
    """, rows)


def insert_links(cur, source_path: str, links: list[str], note_index: NoteIndex):
    rows = [(source_path, lk, note_index.resolve(source_path, lk)) for lk in links]
    if rows:
        cur.executemany(
            f"INSERT INTO {SCHEMA}.links (source_path, target_link, target_path) VALUES (%s,%s,%s);",
            rows,
        )


def index_all(conn):
    print(f"[*] 전체 재인덱싱 시작 — vault={VAULT_PATH}, model={EMBED_MODEL}({EMBED_DIM}d), batch={BATCH}")
    note_index = NoteIndex(VAULT_PATH)
    chunks = build_chunks(VAULT_PATH)
    total = len(chunks)
    print(f"[*] 청크 {total}개 / 노트 {len({c.source_path for c in chunks})}개")

    reset_tables(conn)

    done = 0
    links_per_note: dict[str, set[str]] = {}
    with conn.cursor() as cur:
        for start in range(0, total, BATCH):
            batch = chunks[start:start + BATCH]
            embs = embed_batch([c.text for c in batch])
            rows = [
                (c.source_path, c.title, c.heading_trail, c.heading, c.level, c.text, e)
                for c, e in zip(batch, embs)
            ]
            insert_chunks(cur, rows)
            conn.commit()
            done += len(batch)
            # 진행 체킹: 방금까지 실제 적재된 행 수를 DB에서 재확인
            cur.execute(f"SELECT count(*) FROM {SCHEMA}.chunks;")
            db_cnt = cur.fetchone()[0]
            flag = "✓" if db_cnt == done else f"⚠ DB={db_cnt}"
            print(f"    [{done}/{total}] commit {flag}")
            for c in batch:                          # 링크는 노트별 병합 (뒤에서 일괄 적재)
                if c.links:
                    links_per_note.setdefault(c.source_path, set()).update(c.links)

        # 링크 적재 (전체 노트 인덱스 확보된 뒤 resolve)
        n_links = n_resolved = 0
        for sp, lset in links_per_note.items():
            lks = sorted(lset)
            insert_links(cur, sp, lks, note_index)
            n_links += len(lks)
        conn.commit()
        cur.execute(f"SELECT count(*), count(target_path) FROM {SCHEMA}.links;")
        n_links, n_resolved = cur.fetchone()

    print(f"[*] 완료 — 청크 {done}개, 링크 {n_links}개 (resolve 성공 {n_resolved}개 / 실패 {n_links - n_resolved}개)")


def delete_file(conn, rel_path: str):
    """vault에서 삭제/리네임된 노트의 청크·링크 제거 (incremental 재인덱싱용)."""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {SCHEMA}.chunks WHERE source_path=%s;", (rel_path,))
        n = cur.rowcount
        cur.execute(f"DELETE FROM {SCHEMA}.links WHERE source_path=%s;", (rel_path,))
        conn.commit()
    print(f"[*] {rel_path} 삭제 반영 완료 (청크 {n}개 제거).")


def index_file(conn, rel_path: str):
    full = VAULT_PATH / rel_path
    if not full.exists():
        print(f"[!] 파일 없음: {full}")
        return
    ensure_tables(conn)
    note_index = NoteIndex(VAULT_PATH)
    chunks = chunk_note(rel_path, full.read_text(encoding="utf-8"))
    print(f"[*] 증분 인덱싱: {rel_path} — 청크 {len(chunks)}개")
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {SCHEMA}.chunks WHERE source_path=%s;", (rel_path,))
        cur.execute(f"DELETE FROM {SCHEMA}.links  WHERE source_path=%s;", (rel_path,))
        for start in range(0, len(chunks), BATCH):
            batch = chunks[start:start + BATCH]
            embs = embed_batch([c.text for c in batch])
            insert_chunks(cur, [
                (c.source_path, c.title, c.heading_trail, c.heading, c.level, c.text, e)
                for c, e in zip(batch, embs)
            ])
        all_links = sorted({lk for c in chunks for lk in c.links})
        insert_links(cur, rel_path, all_links, note_index)
        conn.commit()
    print(f"[*] {rel_path} 증분 완료.")


# ---------------------------------------------------------------- 검색 (씨앗 → 1-hop → 노트 union + 이웃 예약)
def search(conn, query: str, seed_k: int, max_neighbors: int,
           max_notes: int, neighbor_reserve: int):
    """벡터 씨앗 → 1-hop 이웃 → **노트 단위 union** 반환 (청크 top-k 칼질 폐기).

    설계 배경 (→ vault search-pipeline.md 2026-07-20):
      옛 방식은 씨앗+이웃 청크를 *같은 쿼리-코사인*으로 재정렬해 top_k를 잘랐음.
      seed_k(10) ≥ top_k(5)면 이웃은 씨앗을 코사인으로 못 이겨 **최종에 절대 진입 못 함**
      (증명된 no-op). 반환값이 노트 경로뿐이고 실제 읽기는 Claude Code가 로컬 전문을
      Read 하므로, 청크 top-k로 좁힐 이유가 없음 → 노트 union으로 전환.

    선택 규칙 (이웃 슬롯 예약 = floor):
      - 노트 점수 = 그 노트 청크들의 best(최고) 코사인.
      - 이웃에게 최소 neighbor_reserve칸 보장(있는 만큼). 나머지는 씨앗+잔여이웃을
        점수순 그리디로 채움 → 총 max_notes까지. 빈칸 낭비 없음(한쪽 부족하면 다른 쪽 흡수).
    """
    qvec = embed_one(query)
    with conn.cursor() as cur:
        # ① 벡터 씨앗: top seed_k 청크 → 씨앗 노트(순서 보존 dedup)
        cur.execute(f"""
            SELECT source_path FROM {SCHEMA}.chunks
            ORDER BY embedding <=> %s::vector LIMIT %s;
        """, (qvec, seed_k))
        seed_paths = list(dict.fromkeys(r[0] for r in cur.fetchall()))
        if not seed_paths:
            return []

        # ② 1-hop 이웃 노트 (outgoing resolve + incoming), 씨앗 제외, 상한
        cur.execute(f"""
            SELECT DISTINCT target_path FROM {SCHEMA}.links
            WHERE source_path = ANY(%s) AND target_path IS NOT NULL
            UNION
            SELECT DISTINCT source_path FROM {SCHEMA}.links
            WHERE target_path = ANY(%s)
        """, (seed_paths, seed_paths))
        seed_set = set(seed_paths)
        neighbor_paths = [r[0] for r in cur.fetchall() if r[0] not in seed_set][:max_neighbors]

        # ③ 후보 노트별 대표 청크 = 최고 코사인 청크 + 그 메타 (노트 점수용)
        cand_paths = seed_paths + neighbor_paths
        cur.execute(f"""
            SELECT DISTINCT ON (source_path)
                   source_path, title, heading_trail, heading, text,
                   (1 - (embedding <=> %s::vector)) AS sim
            FROM {SCHEMA}.chunks
            WHERE source_path = ANY(%s)
            ORDER BY source_path, embedding <=> %s::vector;
        """, (qvec, cand_paths, qvec))
        best = {r[0]: r for r in cur.fetchall()}   # source_path -> row

    def rec(sp: str, is_seed: bool) -> dict:
        _, title, trail, heading, text, sim = best[sp]
        return {
            "source_path": sp,
            "title": title,
            "heading_trail": trail,
            "similarity": round(float(sim), 4),
            "via": "vector" if is_seed else "graph-1hop",
            "snippet": " ".join(text.split())[:220],
            "_score": float(sim),
        }

    seeds = sorted((rec(p, True) for p in seed_paths if p in best),
                   key=lambda x: x["_score"], reverse=True)
    neighs = sorted((rec(p, False) for p in neighbor_paths if p in best),
                    key=lambda x: x["_score"], reverse=True)

    # ④ 이웃 슬롯 예약(floor) + 낭비 없는 그리디 채움
    # 예약은 예산의 절반까지만 — 하한(floor)이 씨앗의 상한(ceiling)으로 변질되는 걸 막는 가드레일.
    # (max_notes 가 작으면 min(reserve, max_notes) 는 예약이 전 슬롯을 먹어 코사인 1위마저 탈락시킴)
    reserve = min(neighbor_reserve, max_notes // 2)
    guaranteed = neighs[:reserve]                          # 이웃 최소 보장분
    guaranteed_ids = {r["source_path"] for r in guaranteed}
    rest_pool = sorted(
        (r for r in (seeds + neighs) if r["source_path"] not in guaranteed_ids),
        key=lambda x: x["_score"], reverse=True,
    )
    rest = rest_pool[: max(0, max_notes - len(guaranteed))]
    selected = sorted(guaranteed + rest, key=lambda x: x["_score"], reverse=True)

    results = []
    for rank, r in enumerate(selected, 1):
        r.pop("_score", None)
        results.append({"rank": rank, **r})
    return results


def print_human(query: str, results: list[dict]):
    print(f"\n[검색] {query}")
    if not results:
        print("  결과 없음.")
        return
    for r in results:
        tag = "🎯벡터" if r["via"] == "vector" else "🌐1-hop"
        print(f"\n[{r['rank']}] {tag}  sim={r['similarity']}  {r['title']} > {r['heading_trail']}")
        print(f"    📄 {r['source_path']}")
        print(f"    {r['snippet']}…")
    print(f"\n→ Claude Code: 위 source_path 들의 로컬 vault 원문을 읽어 종합.")


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="vault-rag 임베딩·검색 (bge-m3 + pgvector)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="전체 재인덱싱 (테이블 리셋)")
    g.add_argument("--file", type=str, help="특정 파일 증분 인덱싱 (추가/수정)")
    g.add_argument("--delete", type=str, help="특정 파일 삭제 반영 (vault에서 지워지거나 리네임된 노트)")
    g.add_argument("--query", type=str, help="검색")
    ap.add_argument("--seed-k", type=int, default=10, help="벡터 씨앗 청크 수 (넓은 그물=recall)")
    ap.add_argument("--max-neighbors", type=int, default=10, help="1-hop 이웃 노트 상한")
    ap.add_argument("--max-notes", type=int, default=10, help="최종 반환 노트 상한 (cap)")
    ap.add_argument("--neighbor-reserve", type=int, default=3, help="이웃에게 보장할 최소 슬롯 (floor)")
    ap.add_argument("--top-k", type=int, default=None, help="(별칭) --max-notes 와 동일. 하위호환용")
    ap.add_argument("--json", action="store_true", help="검색 결과를 JSON 으로")
    args = ap.parse_args()
    max_notes = args.top_k if args.top_k is not None else args.max_notes

    try:
        conn = get_db_connection()
    except Exception as e:
        sys.exit(f"[!] DB 연결 실패: {e}\n    .env(PGHOST 등)와 prod infra 상태 확인.")
    try:
        if args.all:
            index_all(conn)
        elif args.file:
            index_file(conn, args.file)
        elif args.delete:
            delete_file(conn, args.delete)
        elif args.query:
            res = search(conn, args.query, args.seed_k, args.max_neighbors,
                         max_notes, args.neighbor_reserve)
            if args.json:
                print(json.dumps({"query": args.query, "results": res}, ensure_ascii=False, indent=2))
            else:
                print_human(args.query, res)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
