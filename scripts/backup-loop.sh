#!/usr/bin/env bash
# Backups periodicos con Percona XtraBackup: 1 full cada FULL_EVERY ciclos, el resto incrementales.
set -u
INTERVAL=${BACKUP_INTERVAL:-60}
FULL_EVERY=${FULL_EVERY:-5}
KEEP_CHAINS=${KEEP_CHAINS:-10}
HOST=${MYSQL_HOST:-mysql}
PASS=${MYSQL_ROOT_PASSWORD:-lab05root}
DIR=/backups
LOG=$DIR/backup.log
mkdir -p "$DIR"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "$LOG"; }

# Retencion: si hay mas de KEEP_CHAINS fulls, borra el mas viejo y sus incrementales
prune() {
  mapfile -t fulls < <(ls -1d "$DIR"/*_full 2>/dev/null | sort)
  local excess=$(( ${#fulls[@]} - KEEP_CHAINS ))
  for (( i=0; i<excess; i++ )); do
    local base; base=$(basename "${fulls[$i]}")
    for d in "$DIR"/*_incr; do
      [[ -f "$d/lab05_meta" ]] && grep -q "^base=$base$" "$d/lab05_meta" && rm -rf "$d"
    done
    rm -rf "${fulls[$i]}"
    log "RETENCION: eliminada cadena $base"
  done
}

log "backup-loop iniciado: intervalo=${INTERVAL}s full_cada=${FULL_EVERY} cadenas=${KEEP_CHAINS}"
n=0; last=""; base=""
while true; do
  t0=$(date +%s)
  name=$(date -u +%Y%m%dT%H%M%SZ)
  if [[ -z "$last" || $(( n % FULL_EVERY )) -eq 0 ]]; then
    type=full; target="$DIR/${name}_full"; extra=()
  else
    type=incr; target="$DIR/${name}_incr"; extra=(--incremental-basedir="$last")
  fi
  log "INICIO backup $type -> $(basename "$target")"
  if xtrabackup --backup --host="$HOST" --user=root --password="$PASS" \
       --datadir=/var/lib/mysql --target-dir="$target" ${extra[@]+"${extra[@]}"} \
       > "$DIR/.${name}.log" 2>&1; then
    mv "$DIR/.${name}.log" "$target/xtrabackup_backup.log"
    [[ $type == full ]] && base=$(basename "$target")
    read -r bf bp _ < "$target/xtrabackup_binlog_info"
    {
      echo "type=$type"
      echo "base=$base"
      echo "parent=$( [[ $type == incr ]] && basename "$last" )"
      echo "finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "binlog_file=$bf"
      echo "binlog_pos=$bp"
    } > "$target/lab05_meta"
    chmod -R a+rX "$target"
    last="$target"; n=$(( n + 1 ))
    log "OK    backup $type $(basename "$target") binlog=$bf:$bp tamano=$(du -sh "$target" | cut -f1) duracion=$(( $(date +%s) - t0 ))s"
    prune
  else
    log "ERROR backup $type fallo, ver $DIR/.${name}.log"
    rm -rf "$target"
    [[ $type == incr ]] && last=""   # si falla un incremental, el proximo es full
  fi
  wait_s=$(( INTERVAL - ( $(date +%s) - t0 ) ))
  sleep $(( wait_s > 5 ? wait_s : 5 ))
done