"""
vault-rag Phase 1 POC — 임베딩 및 Postgres(pgvector) 적재

이 스크립트는 옵시디언 vault의 청크들을 임베딩 모델(Ollama)을 사용해 벡터화하고,
공용 Postgres 인프라(pgvector)에 적재 및 검색하는 기능을 담당합니다.

사용:
    # 1) 전체 인덱싱 (기존 데이터 초기화 후 전체 적재)
    python src/embed.py --all

    # 2) 특정 파일 증분 인덱싱 (해당 파일의 청크/링크만 삭제 후 갱신)
    python src/embed.py --file "01_Projects/01_Milvus/001.Adapt/readme.md"

    # 3) 질문 검색 테스트
    python src/embed.py --query "Milvus 관련 인프라 설정" --top-k 5
"""
import argparse
import os
import sys
from pathlib import Path
import requests
import psycopg

# chunk.py에서 Chunk 데이터클래스 및 파싱 함수 가져오기
from chunk import Chunk, chunk_note, build_chunks

def load_env(base_dir: Path):
    """.env 파일을 수동 파싱하여 환경 변수에 로드 (python-dotenv 미설치 대비)"""
    env_path = base_dir / ".env"
    if not env_path.exists():
        print(f"[!] {env_path} 파일이 존재하지 않습니다. .env.example을 참고하여 생성해주세요.")
        sys.exit(1)
    
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            # 따옴표 제거
            v_val = v.strip().strip("'\"")
            os.environ[k.strip()] = v_val

# 환경변수 로드
BASE_DIR = Path(__file__).resolve().parent.parent
load_env(BASE_DIR)

VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/Users/rimsm/second-brain"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")

# DB 접속 설정
PGHOST = os.environ.get("PGHOST", "localhost")
PGPORT = os.environ.get("PGPORT", "5432")
PGDATABASE = os.environ.get("PGDATABASE", "vault_rag")
PGUSER = os.environ.get("PGUSER", "vault_rag_app")
PGPASSWORD = os.environ.get("PGPASSWORD", "")

def get_db_connection():
    """Postgres 데이터베이스 커넥션 반환"""
    conn_str = f"host={PGHOST} port={PGPORT} dbname={PGDATABASE} user={PGUSER} password={PGPASSWORD}"
    return psycopg.connect(conn_str)

def get_embedding(text: str, is_query: bool = False) -> list[float]:
    """Ollama API를 통해 nomic-embed-text 임베딩 벡터 생성"""
    prefix = "search_query: " if is_query else "search_document: "
    payload = {
        "model": EMBED_MODEL,
        "prompt": prefix + text
    }
    response = requests.post(f"{OLLAMA_URL.rstrip('/')}/api/embeddings", json=payload, timeout=30)
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as he:
        raise Exception(f"Ollama HTTP {response.status_code} Error: {response.text}") from he
    except Exception as e:
        raise Exception(f"Ollama request failed: {e}") from e
    
    return response.json()["embedding"]

def init_db(conn):
    """필요한 스키마 및 테이블 생성"""
    with conn.cursor() as cur:
        # 스키마는 01-init.sh에서 생성하지만, 확인용으로 둠
        cur.execute("CREATE SCHEMA IF NOT EXISTS second_brain AUTHORIZATION vault_rag_app;")
        
        # chunks 테이블 생성 (pgvector)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS second_brain.chunks (
                id SERIAL PRIMARY KEY,
                source_path TEXT NOT NULL,
                title TEXT NOT NULL,
                heading_trail TEXT NOT NULL,
                heading TEXT NOT NULL,
                level INT NOT NULL,
                text TEXT NOT NULL,
                embedding vector(768) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # links 테이블 생성 (위키링크 관계)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS second_brain.links (
                id SERIAL PRIMARY KEY,
                source_path TEXT NOT NULL,
                target_link TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 인덱스 생성
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source_path ON second_brain.chunks(source_path);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_links_source_path ON second_brain.links(source_path);")
        conn.commit()
        print("[*] DB 스키마 및 테이블 초기화 완료.")

def delete_file_data(cur, rel_path: str):
    """특정 파일의 이전 청크 및 링크 정보 삭제"""
    cur.execute("DELETE FROM second_brain.chunks WHERE source_path = %s;", (rel_path,))
    cur.execute("DELETE FROM second_brain.links WHERE source_path = %s;", (rel_path,))

def insert_chunk(cur, chunk: Chunk, embedding: list[float]):
    """청크 데이터 삽입"""
    cur.execute("""
        INSERT INTO second_brain.chunks (source_path, title, heading_trail, heading, level, text, embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s);
    """, (
        chunk.source_path,
        chunk.title,
        chunk.heading_trail,
        chunk.heading,
        chunk.level,
        chunk.text,
        embedding
    ))

def insert_links(cur, source_path: str, links: list[str]):
    """링크 관계 데이터 삽입"""
    for link in links:
        cur.execute("""
            INSERT INTO second_brain.links (source_path, target_link)
            VALUES (%s, %s);
        """, (source_path, link))

def index_all(conn):
    """전체 마크다운 파일 인덱싱 (TRUNCATE 후 전체 적재)"""
    print(f"[*] 전체 인덱싱을 시작합니다. Vault 경로: {VAULT_PATH}")
    chunks = build_chunks(VAULT_PATH)
    total_chunks = len(chunks)
    print(f"[*] 발견된 총 청크 수: {total_chunks}")
    
    with conn.cursor() as cur:
        # 기존 데이터 초기화
        cur.execute("TRUNCATE TABLE second_brain.chunks RESTART IDENTITY;")
        cur.execute("TRUNCATE TABLE second_brain.links RESTART IDENTITY;")
        
        # 고유 파일 목록 관리 (링크 테이블 적재용)
        inserted_links = set()
        
        for idx, chunk in enumerate(chunks, 1):
            print(f"\r -> 임베딩 처리 중... [{idx}/{total_chunks}]", end="")
            sys.stdout.flush()
            
            try:
                # 임베딩 벡터 구하기
                embedding = get_embedding(chunk.text, is_query=False)
            except Exception as e:
                print(f"\n[!] 임베딩 생성 오류 발생!")
                print(f"    - 파일 경로 : {chunk.source_path}")
                print(f"    - 헤더 경로 : {chunk.heading_trail}")
                print(f"    - 텍스트 길이: {chunk.n_chars} 글자")
                print(f"    - 오류 내용  : {e}")
                sys.exit(1)
            
            # 청크 삽입
            insert_chunk(cur, chunk, embedding)
            
            # 링크 관계 삽입 (파일당 1회씩만 links 삽입)
            link_key = (chunk.source_path, tuple(chunk.links))
            if chunk.links and link_key not in inserted_links:
                insert_links(cur, chunk.source_path, chunk.links)
                inserted_links.add(link_key)
                
        conn.commit()
        print(f"\n[*] 전체 인덱싱 완료! {total_chunks}개 청크 적재됨.")

def index_file(conn, rel_path: str):
    """특정 파일 증분 인덱싱"""
    full_path = VAULT_PATH / rel_path
    if not full_path.exists():
        print(f"[!] 파일을 찾을 수 없습니다: {full_path}")
        return
        
    print(f"[*] 증분 인덱싱 시작: {rel_path}")
    try:
        text = full_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[!] 파일 읽기 실패: {e}")
        return
        
    chunks = chunk_note(rel_path, text)
    print(f"[*] 분할된 청크 수: {len(chunks)}")
    
    with conn.cursor() as cur:
        # 기존 해당 파일 데이터 제거
        delete_file_data(cur, rel_path)
        
        # 새로운 정보 적재
        for chunk in chunks:
            embedding = get_embedding(chunk.text, is_query=False)
            insert_chunk(cur, chunk, embedding)
            
        # 링크 정보 적재 (중복 방지를 위해 병합 후 1회 적재)
        all_links = sorted({link for c in chunks for link in c.links})
        if all_links:
            insert_links(cur, rel_path, all_links)
            
        conn.commit()
        print(f"[*] {rel_path} 증분 인덱싱 완료.")

def search_query(conn, query: str, top_k: int):
    """질문 검색 및 그래프 1-hop 확장 정보 제공"""
    print(f"\n[*] 검색 질문: '{query}'")
    
    # 1. 질문 임베딩 생성
    query_vector = get_embedding(query, is_query=True)
    
    # 2. pgvector 코사인 유사도 기반 벡터 검색
    with conn.cursor() as cur:
        # vector <=> vector는 코사인 거리를 반환하므로, 1 - (거리)로 코사인 유사도를 구함
        cur.execute("""
            SELECT source_path, title, heading_trail, text, (1 - (embedding <=> %s::vector)) AS similarity
            FROM second_brain.chunks
            ORDER BY embedding <=> %s::vector
            LIMIT %s;
        """, (query_vector, query_vector, top_k))
        
        results = cur.fetchall()
        
        if not results:
            print("[!] 검색 결과가 없습니다.")
            return
            
        print(f"\n--- 벡터 검색 Top-{len(results)} 결과 ---")
        source_paths = []
        titles = []
        for idx, row in enumerate(results, 1):
            source_path, title, heading_trail, text, similarity = row
            source_paths.append(source_path)
            titles.append(title)
            print(f"\n[{idx}] {title} > {heading_trail} (유사도: {similarity:.4f})")
            print(f"파일 경로: {source_path}")
            # 본문 일부 출력
            preview = text[:250].replace("\n", " ")
            print(f"미리보기: {preview}...")
            
        # 3. 그래프 1-hop 확장 (SQL JOIN 및 조건문 사용)
        # outgoing links: 검색 결과의 노트 파일들이 직접 링크하고 있는 타겟 명칭 목록
        cur.execute("""
            SELECT DISTINCT target_link
            FROM second_brain.links
            WHERE source_path = ANY(%s)
            LIMIT 15;
        """, (source_paths,))
        out_links = [r[0] for r in cur.fetchall()]
        
        # incoming links: 검색 결과의 노트를 링크하고 있는 다른 노트 파일들
        cur.execute("""
            SELECT DISTINCT source_path
            FROM second_brain.links
            WHERE target_link = ANY(%s)
            LIMIT 15;
        """, (titles,))
        in_links = [r[0] for r in cur.fetchall()]
        
        print("\n--- 🌐 그래프 1-hop 연관 정보 확장 ---")
        if out_links:
            print(f"🔗 이 문서들이 가리키는 외부 대상(Outgoing): {', '.join(out_links)}")
        else:
            print("🔗 이 문서들이 가리키는 외부 대상(Outgoing): 없음")
            
        if in_links:
            print(f"📥 이 문서들을 참조하는 다른 문서들(Incoming): {', '.join(in_links)}")
        else:
            print("📥 이 문서들을 참조하는 다른 문서들(Incoming): 없음")

def main():
    ap = argparse.ArgumentParser(description="vault-rag 임베딩 및 Postgres(pgvector) 적재 파이프라인")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="전체 노트 인덱싱 실행 (기존 데이터 초기화)")
    group.add_argument("--file", type=str, help="특정 파일만 증분 인덱싱 실행 (예: 01_Projects/topic/note.md)")
    group.add_argument("--query", type=str, help="질문 텍스트 입력하여 시맨틱 검색 테스트")
    ap.add_argument("--top-k", type=int, default=3, help="검색 결과 개수")
    
    args = ap.parse_args()
    
    try:
        conn = get_db_connection()
    except Exception as e:
        print(f"[!] 데이터베이스 연결 실패: {e}")
        print("[!] .env 설정과 Docker 컨테이너(infra-postgres) 상태를 확인해주세요.")
        sys.exit(1)
        
    try:
        init_db(conn)
        
        if args.all:
            index_all(conn)
        elif args.file:
            index_file(conn, args.file)
        elif args.query:
            search_query(conn, args.query, args.top_k)
            
    finally:
        conn.close()

if __name__ == "__main__":
    main()
