# Kriisibot image: code lives in /app, state (settings.yaml, events.db, reticulum_identity/)
# in the working directory, which is meant to be a volume.
FROM python:3.13-slim

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && groupadd --gid 1002 reticulum \
    && useradd --uid 1002 --gid 1002 --home-dir /home/reticulum --shell /usr/sbin/nologin reticulum \
    && mkdir -p /home/reticulum/kriisibot \
    && chown -R 1002:1002 /home/reticulum

COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/kriisibot-entrypoint
COPY *.py settings.yaml /app/

USER 1002:1002
ENV HOME=/home/reticulum PYTHONUNBUFFERED=1
WORKDIR /home/reticulum/kriisibot
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/kriisibot-entrypoint"]
CMD ["python3", "/app/main.py"]
