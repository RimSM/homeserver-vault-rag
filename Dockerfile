# vault-rag serve.py — HTTP 검색 엔드포인트 (127.0.0.1:8787 안에서 리슨)
# 표준 라이브러리 http.server 기반, 의존성은 requirements.txt(psycopg/pgvector/requests)뿐.
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# 컨테이너 안에서는 0.0.0.0 으로 리슨해야 밖(호스트)에서 포트매핑으로 닿는다.
# "127.0.0.1 고정 바인드" 요구사항은 host 쪽 포트publish를 127.0.0.1:8787:8787로
# 제한하는 걸로 대신 만족시킨다 (docker-compose.vault-rag.yml 참고).
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "src/serve.py", "--host", "0.0.0.0", "--port", "8787"]
