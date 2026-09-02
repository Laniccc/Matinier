FROM python:3.12-alpine

RUN addgroup -S plugin && adduser -S -G plugin plugin
WORKDIR /app
COPY --chown=plugin:plugin plugin.py /app/plugin.py
USER plugin

ENTRYPOINT ["python", "-u", "/app/plugin.py"]

