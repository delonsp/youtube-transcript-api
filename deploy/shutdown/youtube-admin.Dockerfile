ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY --chmod=755 cron-supervisor.py /app/cron-supervisor.py
COPY --chmod=755 entrypoint-cron.sh /app/entrypoint-cron.sh
