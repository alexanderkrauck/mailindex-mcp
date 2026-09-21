FROM python:3.11-alpine

# The MCP Registry verifies image ownership by reading this annotation and
# matching it against the name in server.json. Without it, publishing is
# rejected as unverifiable.
LABEL io.modelcontextprotocol.server.name="io.github.alexanderkrauck/mailindex-mcp"
LABEL org.opencontainers.image.source="https://github.com/alexanderkrauck/mailindex-mcp"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.description="A mail client for an AI agent: search, move, mark, delete and draft real mail over a self-hosted index."

# OCR languages baked into the image. Tesseract cannot read a script it has no
# data for, and with no language it assumes English and mangles every accented
# word, so the default covers the Latin-script languages most mail is written
# in. Override to add or trim, for example:
#   docker build --build-arg TESSERACT_LANGS="eng deu chi_sim jpn" .
# `apk search tesseract-ocr-data` lists all 67 available packs.
ARG TESSERACT_LANGS="eng deu fra ita spa nld por"

# System dependencies
# - postgresql-dev: needed for psycopg2
# - tesseract-ocr: OCR for image attachments
RUN apk add --no-cache \
    gcc musl-dev libffi-dev openssl-dev python3-dev curl tini \
    postgresql-dev \
    tesseract-ocr leptonica py3-pillow \
    $(for lang in $TESSERACT_LANGS; do echo "tesseract-ocr-data-$lang"; done) \
    && rm -rf /var/cache/apk/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic/ ./alembic/
COPY src/ ./src/
# Read-only post-deploy verification, run with `docker compose exec`.
COPY scripts/ ./scripts/

# Create non-root user
RUN adduser -D -u 1000 emailserver \
    && mkdir -p /data \
    && chown -R emailserver:emailserver /data
USER emailserver

EXPOSE 8000 2525

ENV EMAILSERVER_DATABASE_URL=postgresql://emailserver:emailserver@postgres:5432/emailserver \
    FASTMCP_HOME=/data/fastmcp \
    PYTHONUNBUFFERED=1

# Reap terminated sync/OCR descendants. src.main supervises the API process;
# Docker's restart policy handles a watchdog-triggered exit, not health alone.
ENTRYPOINT ["/sbin/tini", "--"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl --fail --silent --max-time 4 http://localhost:8000/api/v1/health || exit 1
CMD ["python", "-m", "src.main"]
