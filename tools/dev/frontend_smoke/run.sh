#!/usr/bin/env bash
# 一键：建库(已存在则复用)→迁移→起后端(8799)+纯静态服务(8798)→Playwright 冒烟→清理进程
# 用法：bash tools/dev/frontend_smoke/run.sh
set -u
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PGB=/opt/homebrew/opt/postgresql@17/bin
DB=rednote_fe_smoke
PORT=${PORT:-8799}; SPORT=${SPORT:-8798}
export NO_PROXY='*' no_proxy='*'
$PGB/psql -lqt | cut -d'|' -f1 | grep -qw $DB || { $PGB/createdb $DB && $PGB/psql -d $DB -c 'CREATE EXTENSION vector' >/dev/null; }
export REDNOTE_DATABASE_URL="postgresql+psycopg://$(whoami)@127.0.0.1:5432/$DB"
export REDNOTE_ENVIRONMENT=dev REDNOTE_FRONTEND_STATIC_DIR="$ROOT/services/backend/static"
export REDNOTE_OIDC_TOKEN_ENCRYPTION_KEY="$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
PY="$ROOT/services/backend/.venv/bin/python"
(cd "$ROOT" && "$PY" -m alembic -c migrations/alembic.ini upgrade head >/tmp/fe_smoke_alembic.log 2>&1) || { echo "alembic 失败"; exit 2; }
(cd "$ROOT/services/backend" && exec "$PY" -m uvicorn --factory rednote.main:create_app --port $PORT >/tmp/fe_smoke_backend.log 2>&1) &
BP=$!
(cd "$ROOT/services/backend/static" && exec python3 -m http.server $SPORT --bind 127.0.0.1 >/dev/null 2>&1) &
SP=$!
trap 'kill $BP $SP 2>/dev/null' EXIT
for i in $(seq 40); do curl -sf "http://127.0.0.1:$PORT/api/v1/health" >/dev/null && break; sleep 0.5; done
BASE="http://127.0.0.1:$PORT" STATIC_ONLY_BASE="http://127.0.0.1:$SPORT" node "$ROOT/tools/dev/frontend_smoke/smoke.mjs"
