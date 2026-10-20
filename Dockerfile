# The garmin-reader MCP server over HTTP, for a host that gives it a stable HTTPS URL,
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
RUN useradd --create-home --uid 10001 garmin-reader \
    && mkdir /data && chown garmin-reader:garmin-reader /data && chmod 700 /data
USER garmin-reader
VOLUME /data

# Listen on every interface inside the container. The port is $GARMIN_READER_PORT,
# else $PORT (set by platforms such as Cloud Run), else 8000; don't set
# GARMIN_READER_PORT here, or it would override the platform's PORT.
ENV GARMIN_READER_STATE_DIR=/data \
    GARMIN_READER_HOST=0.0.0.0
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os, urllib.request as u; p = os.environ.get('GARMIN_READER_PORT') or os.environ.get('PORT') or '8000'; u.urlopen('http://127.0.0.1:' + p + '/healthz', timeout=4)"

# The commit the image was built from (the image has no git history), for
# get_garmin_reader_info. Last, so changing it doesn't rebuild the layers above.
ARG GARMIN_READER_GIT_COMMIT=
ENV GARMIN_READER_GIT_COMMIT=$GARMIN_READER_GIT_COMMIT

# GARMIN_READER_PUBLIC_URL is required at run time.
CMD ["garmin-reader-mcp", "--http"]
