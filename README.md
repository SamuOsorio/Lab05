# Lab 05 — Backup, Binlog Archiving y Point-in-Time Recovery en MySQL

Laboratorio de Administración de Bases de Datos (Pontificia Universidad Javeriana, Semestre 8) que simula el flujo de protección de datos de una herramienta tipo Rubrik/Veeam para MySQL: ingesta continua de datos, backups automáticos (full + incrementales) con **Percona XtraBackup**, archivado continuo de **binary logs**, inyección de una catástrofe (`DELETE` masivo) y restauración **Point-in-Time (PITR)** combinando el backup más reciente con el replay de binlogs.

Como fuente de datos "en vivo" se usa la salida FCD (*Floating Car Data*) de una simulación de tráfico de **SUMO** sobre el dataset **TAPAS Cologne**: un proceso de Python va leyendo las posiciones de los vehículos a medida que SUMO las escribe e inserta cada timestep en MySQL, emulando un sistema OLTP que recibe escrituras constantes mientras el pipeline de backup corre en paralelo.

## Arquitectura

Todo se orquesta con Docker Compose (`docker-compose.yml`), sobre una red local con un volumen compartido para el datadir de MySQL:

| Servicio | Imagen | Rol |
|---|---|---|
| `mysql` | `mysql:8.0.36` | Base de datos `sumo_db`, con binary log activado (`--log-bin=binlog`, `--binlog-format=ROW`) para poder hacer PITR. |
| `backup` | `percona/percona-xtrabackup:8.0.35-36` | Corre `scripts/backup-loop.sh`: backups periódicos full + incrementales hacia `./backups`. |
| `binlog-archiver` | `mysql:8.0.36-debian` | Corre `scripts/binlog-archiver.sh`: copia continua (streaming) de los binlogs del servidor hacia `./binlogs`, equivalente al "log backup" de un producto de protección de datos. |
| `mysql-restore` | `mysql:8.0.36` | Perfil `restore`. Instancia aislada (puerto `3307`) usada para restaurar un backup y validar el PITR sin tocar el servidor productivo. |
| `tools` | `mysql:8.0.36-debian` | Perfil `tools`. Contenedor utilitario con acceso de solo lectura a `./binlogs` para inspeccionar/aplicar binlogs manualmente (`mysqlbinlog`, `mysql`). |

El código de la aplicación vive fuera de los contenedores:

- `ingest.py` — ingesta en tiempo real + inyección de la catástrofe.
- `TAPASCologne-0.32.0/` — escenario de simulación de SUMO (red vial de Colonia, viajes, tipos de vehículo).
- `.venv/` — entorno virtual con SUMO y sus binarios (`sumo`, `sumo-gui`, etc.) y `pymysql`.

## Estrategia de backups (`scripts/backup-loop.sh`)

Corre dentro del contenedor `backup` en un loop infinito, usando `xtrabackup --backup` contra el MySQL productivo:

- Cada `BACKUP_INTERVAL` segundos (default **60s**) toma un backup.
- 1 de cada `FULL_EVERY` backups (default **5**) es **full**; el resto son **incrementales** (`--incremental-basedir` contra el backup anterior), encadenados a partir del último full (`base=`).
- Cada backup queda en `backups/<timestamp>_full|incr/`, con metadata propia en `lab05_meta` (tipo, full base, parent, posición de binlog al finalizar) y log agregado en `backups/backup.log`.
- **Retención**: si hay más de `KEEP_CHAINS` cadenas full (default **10**), se elimina la cadena full más antigua junto con todos sus incrementales asociados.
- Si un incremental falla, la cadena se corta y el siguiente backup se fuerza a `full`.

## Archivado continuo de binlogs (`scripts/binlog-archiver.sh`)

Corre en el contenedor `binlog-archiver` y usa `mysqlbinlog --read-from-remote-server --raw --stop-never` para ir copiando los binary logs del servidor a `./binlogs` en tiempo real a medida que se generan (no espera a que el archivo se cierre). Si pierde la conexión reintenta cada 3s, retomando desde el último binlog ya archivado. Esto es lo que permite hacer **PITR**: replays finos a nivel de transacción entre dos backups físicos.

## Ingesta y catástrofe simulada (`ingest.py`)

```bash
python ingest.py TAPASCologne-0.32.0/locations.xml [--reset] \
    [--delete-at SEG] [--delete-from SEG] [--delete-to SEG] [--evidence-dir evidence]
```

- Se conecta a MySQL (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASS`, `DB_NAME`, default `root/lab05root@127.0.0.1:3306/sumo_db`) y crea la tabla `locations` (posición, velocidad, carril, etc., indexada por `sim_time`, `vehicle_id` e `ingested_at`).
- Sigue `locations.xml` estilo `tail -f` mientras SUMO lo va escribiendo, y hace un **commit por timestep** (lote de vehículos) insertado con `executemany`.
- Si se pasa `--delete-at`, al llegar a ese segundo de simulación ejecuta un `DELETE FROM locations WHERE sim_time BETWEEN --delete-from AND --delete-to` (catástrofe controlada) y guarda evidencia en `evidence/catastrophe.json`: SQL ejecutado, filas afectadas, hora UTC real y la **posición exacta del binlog antes y después del DELETE** (clave para saber hasta dónde hay que hacer el PITR).

## Puesta en marcha

1. **Levantar la infraestructura:**
   ```bash
   docker compose up -d mysql backup binlog-archiver
   ```
2. **Generar la simulación de SUMO** (produce `locations.xml` incrementalmente):
   ```bash
   source .venv/bin/activate
   sumo -c TAPASCologne-0.32.0/cologne.sumocfg \
        --fcd-output TAPASCologne-0.32.0/locations.xml --fcd-output.geo
   ```
3. **Ingestar en paralelo**, programando el DELETE catastrófico en un punto conocido de la simulación:
   ```bash
   python ingest.py TAPASCologne-0.32.0/locations.xml --reset \
       --delete-at 8000 --delete-from 4000 --delete-to 6000
   ```
   Mientras tanto, `backup` y `binlog-archiver` siguen tomando backups y archivando binlogs sin intervención manual.

## Restauración Point-in-Time (PITR)

Objetivo: reconstruir la base justo **antes** del `DELETE` inyectado, perdiendo la menor cantidad de datos posible.

1. **Identificar el punto de corte** en `evidence/catastrophe.json` → `binlog_before_delete` (archivo + posición inmediatamente anteriores al `DELETE`).
2. **Elegir la cadena de backup** (full + incrementales) más reciente cuyo `binlog_pos` (en `lab05_meta` / `evidence/backups_meta.txt`) sea anterior a ese punto.
3. **Preparar el backup** con `xtrabackup --prepare`:
   - `--apply-log-only` sobre el full y cada incremental excepto el último de la cadena.
   - Prepare final (sin `--apply-log-only`) incorporando el último incremental, dejando el datadir consistente.
4. **Copiar el resultado** a `restore/datadir` y levantar la instancia aislada:
   ```bash
   docker compose --profile restore up -d mysql-restore
   ```
5. **Reproducir los binlogs** desde la posición registrada en el backup (`binlog_file:binlog_pos`) hasta justo antes de `binlog_before_delete`, usando el contenedor `tools`:
   ```bash
   docker compose run --rm tools bash -c \
     "mysqlbinlog --start-position=<pos_backup> --stop-position=<pos_antes_delete> \
      /binlogs/<binlog_file> | mysql -h mysql-restore -uroot -plab05root sumo_db"
   ```
6. **Verificar** que la restauración excluye exactamente las filas borradas y conserva todo lo demás.

## Verificación de resultados

La validación se hizo comparando, para cada segundo de simulación, el conteo de vehículos entre la base productiva y la restaurada:

```bash
mysql ... -e "SELECT sim_time, COUNT(*) FROM locations GROUP BY sim_time" > prod.txt   # productivo
mysql ... -e "SELECT sim_time, COUNT(*) FROM locations GROUP BY sim_time" > rest.txt   # restaurado
diff prod.txt rest.txt
```

Evidencia obtenida en `evidence/` (ver `catastrophe.json`, `backups_meta.txt`, `backup_resumen.log`, `binlog-archiver.log`):

- Catástrofe: `DELETE FROM locations WHERE sim_time BETWEEN 4000 AND 6000` a `sim_time=8000`, **437,175 filas borradas** de un total de 658,138 (binlog `binlog.000007` de la posición `9072644` a `51671179`).
- `diff prod.txt rest.txt` solo difiere en una fila (`6486.00  74`, fuera del rango borrado), correspondiente al último timestep en vuelo en el momento del corte del PITR — es decir, la restauración recuperó el estado de la base con pérdida mínima de datos y excluyendo correctamente las filas de la catástrofe.

## Estructura relevante del repositorio

```
docker-compose.yml          # orquestación de los 5 servicios
ingest.py                   # ingesta FCD -> MySQL + inyección de catástrofe
scripts/
  backup-loop.sh            # backups full/incremental con xtrabackup
  binlog-archiver.sh        # archivado continuo de binlogs
backups/                    # backups físicos (full_*/incr_*) + backup.log
binlogs/                    # binary logs archivados
restore/                    # datadir y workdir usados para la restauración
evidence/                   # evidencia de la catástrofe y de la validación del PITR
TAPASCologne-0.32.0/        # escenario SUMO (red vial, trips, config)
.venv/                      # entorno Python con SUMO y pymysql
```

## Requisitos previos

- Docker y Docker Compose.
- Python 3 con `pymysql` (ya incluido en `.venv/`).
- SUMO (binarios en `.venv/bin/`, incluidos vía el entorno virtual).
