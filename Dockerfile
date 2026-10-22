FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOSTWATCH_CONTAINER=1 \
    HOSTWATCH_INSTALL_DIR=/app \
    HOSTWATCH_DATA_DIR=/data \
    HOSTWATCH_BACKUP_DIR=/backups \
    HOSTWATCH_BACKUP_SETTINGS=/data/backup-schedule.json \
    HOSTWATCH_FLARESOLVERR_URL=http://flaresolverr:8191/v1 \
    HOSTWATCH_USERS_PATH=/data/stockwatch-users.json \
    HOSTWATCH_MANAGER_LOCK=/data/.manager.lock \
    ADMIN_USERS_FILE=/data/stockwatch-users.json \
    VMISS_CONFIG=/data/config.json \
    VMISS_STATUS=/data/status.json \
    VMISS_RATES=/data/exchange-rates.json \
    STATE_FILE=/data/state.json \
    ADMIN_HOST=0.0.0.0 \
    ADMIN_PORT=25678 \
    ADMIN_ALLOW_REMOTE=1

RUN groupadd --system vmiss-monitor && useradd --system --gid vmiss-monitor --home-dir /app --shell /usr/sbin/nologin vmiss-monitor
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data /backups && chown -R vmiss-monitor:vmiss-monitor /app /data /backups && chmod 700 /data /backups && chmod 755 docker/entrypoint.sh
USER vmiss-monitor
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python", "admin.py"]
