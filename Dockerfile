FROM python:3.13-slim

# Standard library only: nothing to install.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HGW_DB=/data/gateway.db

RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin gateway \
 && mkdir /data && chown gateway:gateway /data

COPY hindsight_gateway /app/hindsight_gateway

USER 10001
VOLUME /data
# 8889: the API agents use. 8890: the admin UI (keep it off the agents' network).
EXPOSE 8889 8890
ENTRYPOINT ["python", "-P", "-m", "hindsight_gateway"]
