#!/bin/sh
set -e

# 호스트 .env는 :ro로 /app/.env.host에 마운트됨. 여기서 컨테이너 전용 사본을
# /app/.env(embed.py가 직접 읽는 경로, load_env 참고)에 만들고 PGHOST/OLLAMA_URL만
# 오버라이드한다.
#
# 이유: 호스트 .env의 PGHOST/OLLAMA_URL이 localhost/127.0.0.1(자기 자신 참조)이면
# 컨테이너 안에서는 컨테이너 자기 자신을 가리켜서 못 붙는다(prod 실제로 이 값).
# stage는 이미 Tailscale IP 같은 실주소라 컨테이너 안에서도 그대로 도달하므로 안 건드림.
#
# → localhost/127.0.0.1인 경우에만 host.docker.internal(Docker Desktop이 제공하는
# "호스트 자신" 이름)로 치환. Postgres는 host:5432로 publish돼 있고, Ollama도
# stage=host native/prod=컨테이너 둘 다 host:11434로 도달 가능해서 이 하나로 커버됨.
cp /app/.env.host /app/.env
sed -i \
  -e 's/^PGHOST=\(localhost\|127\.0\.0\.1\)[[:space:]]*$/PGHOST=host.docker.internal/' \
  -e 's#^OLLAMA_URL=http://\(localhost\|127\.0\.0\.1\):#OLLAMA_URL=http://host.docker.internal:#' \
  /app/.env

exec "$@"
