"""
vault-rag Phase 1 POC — 청킹(chunking)

옵시디언 vault의 .md 노트를 '헤더 섹션' 단위로 잘라 청크로 만든다.
프레임워크(LangChain/LlamaIndex) 안 씀 — 순수 파이썬 hand-roll (학습 목적).
표준 라이브러리만 사용 → pip 설치 없이 바로 실행.

사용:
    python src/chunk.py                 # vault 통계 + 샘플 청크
    python src/chunk.py --sample 5      # 샘플 청크 5개
    python src/chunk.py --json > chunks.json

다음 단계(별도 파일): 청크 → Ollama 임베딩 → Postgres(pgvector) 적재.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path

# vault에서 인덱싱 제외할 디렉토리 (설정/휴지통/아카이브 등)
SKIP_DIRS = {".obsidian", ".trash", ".git", "09_Archive"}

HEADER_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")   # 마크다운 헤더
FENCE_RE = re.compile(r"^\s*(```|~~~)")             # 코드펜스 시작/끝
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")         # [[위키링크]] (alias/앵커 앞부분만)


@dataclass
class Chunk:
    source_path: str      # vault 기준 상대 경로
    title: str            # 노트 제목 (첫 H1 또는 파일명)
    heading_trail: str    # 상위 헤더 breadcrumb (예: "현재 상태 > DB 격리")
    heading: str          # 이 청크의 헤더 (intro면 "")
    level: int            # 헤더 레벨 (intro면 0)
    text: str             # 청크 본문 (헤더 포함)
    n_chars: int
    links: list[str]      # 청크 안 [[위키링크]] (나중 그래프 1-hop 확장용)


def iter_markdown_files(vault: Path):
    for root, dirs, files in os.walk(vault):
        # 제외 디렉토리 in-place 필터 (숨김 폴더도 제외)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith(".md"):
                yield Path(root) / name


def strip_frontmatter(text: str) -> str:
    """최상단 YAML frontmatter(--- ... ---) 제거."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            nl = text.find("\n", end + 1)
            return text[nl + 1:] if nl != -1 else ""
    return text


def chunk_note(rel_path: str, text: str) -> list[Chunk]:
    """노트 하나를 헤더 섹션 단위 청크 리스트로."""
    lines = strip_frontmatter(text).splitlines()

    chunks: list[Chunk] = []
    title = ""
    stack: list[tuple[int, str]] = []          # 헤더 breadcrumb 스택
    cur_heading, cur_level, cur_trail = "", 0, ""
    buf: list[str] = []
    in_fence = False

    MAX_CHUNK_CHARS = 2000

    def create_chunk(text_content: str):
        if not text_content.strip():
            return
        chunks.append(Chunk(
            source_path=rel_path,
            title=title or Path(rel_path).stem,
            heading_trail=cur_trail,
            heading=cur_heading,
            level=cur_level,
            text=text_content,
            n_chars=len(text_content),
            links=sorted({m.strip() for m in WIKILINK_RE.findall(text_content)}),
        ))

    def split_large_text(raw_text: str) -> list[str]:
        # raw_text가 MAX_CHUNK_CHARS를 넘는 경우 문단(\n\n) 단위로 쪼갠다.
        paragraphs = raw_text.split("\n\n")
        sub_chunks = []
        current_buf = []
        current_len = 0
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            # 문단 하나 자체가 임계치를 초과하면 글자 수 단위로 강제 분할
            if len(para) > MAX_CHUNK_CHARS:
                if current_buf:
                    sub_chunks.append("\n\n".join(current_buf))
                    current_buf = []
                    current_len = 0
                for i in range(0, len(para), MAX_CHUNK_CHARS):
                    sub_chunks.append(para[i:i+MAX_CHUNK_CHARS])
            elif current_len + len(para) + 2 > MAX_CHUNK_CHARS:
                sub_chunks.append("\n\n".join(current_buf))
                current_buf = [para]
                current_len = len(para)
            else:
                current_buf.append(para)
                current_len += len(para) + 2
                
        if current_buf:
            sub_chunks.append("\n\n".join(current_buf))
            
        return sub_chunks

    def flush():
        nonlocal buf
        raw = "\n".join(buf).strip()
        if raw:
            if len(raw) > MAX_CHUNK_CHARS:
                sub_chunks = split_large_text(raw)
                for sc in sub_chunks:
                    create_chunk(sc)
            else:
                create_chunk(raw)
        buf = []

    for line in lines:
        if FENCE_RE.match(line):               # 코드펜스 안/밖 토글
            in_fence = not in_fence
            buf.append(line)
            continue
        m = None if in_fence else HEADER_RE.match(line)   # 코드블록 안 '#'는 헤더 아님
        if m:
            flush()                            # 이전 섹션 마감 (이전 메타로)
            level = len(m.group(1))
            heading = m.group(2).strip()
            if level == 1 and not title:
                title = heading
            while stack and stack[-1][0] >= level:   # 같거나 얕은 헤더 스택에서 제거
                stack.pop()
            stack.append((level, heading))
            cur_heading, cur_level = heading, level
            cur_trail = " > ".join(h for _, h in stack)
            buf.append(line)                   # 헤더도 청크 본문에 포함
        else:
            buf.append(line)
    flush()
    return chunks


def build_chunks(vault: Path) -> list[Chunk]:
    all_chunks: list[Chunk] = []
    for path in iter_markdown_files(vault):
        rel = str(path.relative_to(vault))
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        all_chunks.extend(chunk_note(rel, text))
    return all_chunks


def main():
    ap = argparse.ArgumentParser(description="vault 노트 헤더 섹션 청킹 (POC)")
    ap.add_argument("--vault", default=os.environ.get("VAULT_PATH", "/Users/rimsm/second-brain"))
    ap.add_argument("--sample", type=int, default=3, help="본문 미리보기 청크 개수")
    ap.add_argument("--json", action="store_true", help="청크 전체를 JSON으로 출력")
    args = ap.parse_args()

    vault = Path(args.vault).expanduser()
    chunks = build_chunks(vault)

    if args.json:
        print(json.dumps([asdict(c) for c in chunks], ensure_ascii=False, indent=2))
        return

    n_notes = len({c.source_path for c in chunks})
    sizes = [c.n_chars for c in chunks]
    total = len(chunks)
    print(f"vault      : {vault}")
    print(f"노트 수    : {n_notes}")
    print(f"청크 수    : {total}")
    if sizes:
        s = sorted(sizes)
        print(f"청크 크기  : min {s[0]} / 중앙 {s[len(s)//2]} / 평균 {sum(s)//total} / max {s[-1]} chars")
        big = [c for c in chunks if c.n_chars > 2000]
        print(f"2000자 초과: {len(big)}개 (임베딩 컨텍스트 넘칠 수 있음 → 나중 재분할 검토)")
    print(f"링크 포함 청크: {sum(1 for c in chunks if c.links)}개")

    print("\n--- 샘플 청크 ---")
    for c in chunks[:args.sample]:
        print(f"\n[{c.source_path}]  trail='{c.heading_trail}'  ({c.n_chars} chars, links={c.links})")
        print(c.text[:300] + ("…" if len(c.text) > 300 else ""))


if __name__ == "__main__":
    main()
