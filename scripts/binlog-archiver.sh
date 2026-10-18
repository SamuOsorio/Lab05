#!/usr/bin/env bash
# Copia continua de los binary logs del servidor (equivalente al "log backup" de Rubrik).
set -u
HOST=${MYSQL_HOST:-mysql}
DIR=/binlogs
mkdir -p "$DIR"
export MYSQL_PWD=${MYSQL_ROOT_PASSWORD:-lab05root}

until mysqladmin ping -h "$HOST" -uroot --silent 2>/dev/null; do sleep 2; done

# arranca desde el ultimo archivado o, si no hay, desde el primer binlog del servidor
first=$(ls -1 "$DIR" 2>/dev/null | grep -E '^binlog\.[0-9]+$' | sort | tail -1)
[[ -z "$first" ]] && first=$(mysql -h "$HOST" -uroot -N -e "SHOW BINARY LOGS" | head -1 | cut -f1)

while true; do
  echo "$(date -u +%FT%TZ) archivando binlogs desde $first -> $DIR"
  mysqlbinlog --read-from-remote-server --host="$HOST" --user=root \
    --raw --stop-never --connection-server-id=100 --result-file="$DIR/" "$first"
  echo "$(date -u +%FT%TZ) mysqlbinlog se detuvo, reintentando en 3s"
  sleep 3
  until mysqladmin ping -h "$HOST" -uroot --silent 2>/dev/null; do sleep 2; done
  first=$(ls -1 "$DIR" | grep -E '^binlog\.[0-9]+$' | sort | tail -1)
done