# The kcal MCP server over HTTP, for a host that gives it a stable HTTPS URL,
# one persistent volume and exactly one running instance.
# See docs/deployment.md.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# All state lives in one volume: each user's Garmin tokens (users/) and the
# OAuth clients, tokens and users (mcp_auth.sqlite). Owner-only, as on a PC.
RUN useradd --create-home --uid 10001 kcal \
    && mkdir /data && chown kcal:kcal /data && chmod 700 /data
USER kcal
VOLUME /data

# Listen on every interface inside the container. The port is $KCAL_PORT,
# else $PORT (set by platforms such as Cloud Run), else 8000; don't set
# KCAL_PORT here, or it would override the platform's PORT.
ENV KCAL_STATE_DIR=/data \
    KCAL_HOST=0.0.0.0
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os, urllib.request as u; p = os.environ.get('KCAL_PORT') or os.environ.get('PORT') or '8000'; u.urlopen('http://127.0.0.1:' + p + '/healthz', timeout=4)"

# KCAL_PUBLIC_URL and KCAL_ALLOWED_EMAILS are required at run time.
CMD ["kcal-mcp", "--http"]
