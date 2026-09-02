FROM python:3.12-alpine

RUN addgroup -S plugin && adduser -S -G plugin plugin
WORKDIR /app
COPY --chown=plugin:plugin plugin-sdk/python/matinier_plugin /app/matinier_plugin
COPY --chown=plugin:plugin plugin-sdk/examples/course-organizer/plugin.py /app/plugin.py
COPY --chown=plugin:plugin plugin-sdk/examples/course-organizer/course_organizer /app/course_organizer
USER plugin

ENTRYPOINT ["python", "-u", "/app/plugin.py"]
