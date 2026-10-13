FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && node --version \
    && npx --version
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 studio \
    && mkdir -p /data/workspace /data/secrets && chown -R studio:studio /data
COPY config.yaml ./config.yaml
COPY scripts/docker-entrypoint.sh /usr/local/bin/newsletter-entrypoint
COPY scripts/migrate_docker_config.py /usr/local/bin/migrate-docker-config
RUN chmod 755 /usr/local/bin/newsletter-entrypoint
USER studio
VOLUME ["/data"]
EXPOSE 8000
ENTRYPOINT ["newsletter-entrypoint"]
