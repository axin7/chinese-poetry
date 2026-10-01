set -eu
cd /opt/poetry-tang-20260922
test -f bin/vector-api.score-20260922
test ! -e bin/vector-api.before-score-20260922
printf '%s\n' \
  'a73433e175d7d5d658b6ddb03af130447b15916a91134585572ed6a494bd8c37  bin/vector-api.score-20260922' \
  | sha256sum --check -
docker compose --env-file .env config --quiet
sha256sum corpus/chinese_poetry.db corpus/datas.json
docker inspect --format '{{.Id}} {{.State.StartedAt}}' poetry-tang-qdrant-1
cp -p bin/vector-api bin/vector-api.before-score-20260922
chmod 755 bin/vector-api.score-20260922
mv bin/vector-api.score-20260922 bin/vector-api
rollback() {
  cp -p bin/vector-api.before-score-20260922 bin/vector-api.rollback
  mv bin/vector-api.rollback bin/vector-api
  docker compose --env-file .env up -d --no-deps --force-recreate vector-api
}
if ! docker compose --env-file .env up -d --no-deps --force-recreate vector-api; then
  rollback
  exit 1
fi
ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if curl --fail --silent http://127.0.0.1:18080/health; then
    ready=1
    break
  fi
  sleep 1
done
if test "$ready" -ne 1; then
  rollback
  exit 1
fi
sha256sum bin/vector-api corpus/chinese_poetry.db corpus/datas.json
docker inspect --format '{{.Id}} {{.State.StartedAt}}' poetry-tang-qdrant-1
curl --fail --silent http://127.0.0.1:17333/collections/poetry_tang_20260922_v1
