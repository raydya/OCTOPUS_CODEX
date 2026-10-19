FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 octopus \
    && useradd --system --uid 10001 --gid octopus --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin octopus \
    && python -m venv /opt/venv

WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN /opt/venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY tests /app/tests
COPY scripts/codex-host.sh scripts/codex_host.py /app/scripts/
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps .

COPY docker/entrypoint.sh /usr/local/bin/octopus-entrypoint
RUN chmod 0755 /usr/local/bin/octopus-entrypoint

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/octopus-entrypoint"]
CMD ["/opt/venv/bin/python", "-m", "octopus", "run", "--wait-for-config"]
