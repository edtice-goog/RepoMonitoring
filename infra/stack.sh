#!/usr/bin/env bash
# RepoMonitoring local datastore stack: USER-OWNED Postgres + Redis in WSL2 (no root).
# Data lives on WSL2 ext4 under ~/repomon; the app connects from Windows over localhost.
# Uses non-default ports (5544/6380) so it never collides with any system cluster.
#
#   ./stack.sh up | down | status | init
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${REPOMON_HOME:-$HOME/repomon}"
PGDATA="$ROOT/pgdata"
REDISDATA="$ROOT/redisdata"
LOGS="$ROOT/logs"
PGPORT="${REPOMON_PGPORT:-5544}"
REDISPORT="${REPOMON_REDISPORT:-6380}"
PGBIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"
PGUSER=repomon
export PGHOST=127.0.0.1 PGPORT PGUSER

[ -n "$PGBIN" ] || { echo "postgres not found under /usr/lib/postgresql/*/bin"; exit 1; }

pg_up()    { "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; }
redis_up() { redis-cli -p "$REDISPORT" ping >/dev/null 2>&1; }

init() {
  mkdir -p "$ROOT" "$REDISDATA" "$LOGS"
  if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "initdb -> $PGDATA (superuser $PGUSER, port $PGPORT)"
    "$PGBIN/initdb" -D "$PGDATA" -U "$PGUSER" --auth=trust --encoding=UTF8 >/dev/null
    {
      echo "listen_addresses = '127.0.0.1'"
      echo "port = $PGPORT"
      echo "unix_socket_directories = '$ROOT'"
    } >> "$PGDATA/postgresql.conf"
  fi
  sed "s|__PORT__|$REDISPORT|g; s|__DIR__|$REDISDATA|g" "$HERE/redis.conf" > "$ROOT/redis.conf"
}

up() {
  init
  pg_up || "$PGBIN/pg_ctl" -D "$PGDATA" -l "$LOGS/pg.log" -w start
  "$PGBIN/psql" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='repomon'" | grep -q 1 \
    || "$PGBIN/createdb" repomon
  redis_up || redis-server "$ROOT/redis.conf"
  sleep 1
  status
  echo
  echo "DATABASE_URL=postgresql+psycopg://$PGUSER@127.0.0.1:$PGPORT/repomon"
  echo "REDIS_URL=redis://127.0.0.1:$REDISPORT/0"
}

down() {
  redis_up && redis-cli -p "$REDISPORT" shutdown 2>/dev/null || true
  pg_up    && "$PGBIN/pg_ctl" -D "$PGDATA" -m fast stop >/dev/null || true
  echo "stopped"
}

status() {
  pg_up    && echo "postgres: UP   127.0.0.1:$PGPORT  ($PGDATA)" || echo "postgres: DOWN"
  redis_up && echo "redis:    UP   127.0.0.1:$REDISPORT  ($REDISDATA)" || echo "redis:    DOWN"
}

case "${1:-up}" in
  up) up;; down) down;; status) status;; init) init;;
  *) echo "usage: $0 {up|down|status|init}"; exit 1;;
esac
