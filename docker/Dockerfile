# Official OpenClaw image (with Chromium). The container only needs python3 for the tiny stdlib-only
# broker clients (ia, nt) — it never gets Google/Notion libraries or keys.
FROM ghcr.io/openclaw/openclaw:2026.10.6-browser

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/*
USER node
ENV PYTHONDONTWRITEBYTECODE=1
