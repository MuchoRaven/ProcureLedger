# syntax=docker/dockerfile:1

# ---------- stage 1: build the UI ----------
FROM node:20-bookworm-slim AS ui
WORKDIR /ui
# Copies ui/ minus node_modules and dist (see .dockerignore).
# If you commit ui/package-lock.json, switch to:
#   COPY ui/package.json ui/package-lock.json ./
#   RUN npm ci
# for a faster, reproducible build.
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build
# ---------- stage 2: runtime ----------
FROM python:3.11-slim-bookworm

# bubblewrap + prlimit (util-linux) are required by sandbox.preflight().
# nginx serves the SPA and proxies the API on the same origin: app.py has no
# CORS middleware, so same-origin is mandatory. curl is for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
      bubblewrap \
      util-linux \
      nginx \
      curl \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# sandbox._bwrap_command() runs `python3` with PATH=/usr/bin:/bin, but the
# slim image puts the interpreter in /usr/local/bin. Without this symlink
# every tool call returns SANDBOX_VIOLATION (exit 127).
RUN ln -sf /usr/local/bin/python3 /usr/bin/python3

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /opt/procureledger

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=ui /ui/dist /opt/procureledger/ui/dist

COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
RUN rm -f /etc/nginx/sites-enabled/default \
    && chmod +x /opt/procureledger/deploy/entrypoint.sh

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1/health || exit 1

CMD ["/opt/procureledger/deploy/entrypoint.sh"]
