FROM python:3.12-alpine
RUN addgroup -S plugin && adduser -S -G plugin plugin
WORKDIR /app
COPY plugin-sdk/python/matinier_plugin /app/matinier_plugin
COPY plugin-sdk/examples/meeting-assistant/plugin.py /app/plugin.py
COPY plugin-sdk/examples/meeting-assistant/meeting_assistant /app/meeting_assistant
USER plugin
CMD ["python", "-u", "/app/plugin.py"]
