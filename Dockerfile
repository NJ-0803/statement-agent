# A container for the public demo (STATEMENT_AGENT_MODE=demo) or a private instance
# (STATEMENT_AGENT_MODE=owner). Deliberately host-agnostic: it runs on Fly, Render, Railway, a VPS
# or anything else that takes an image, so the choice of host is not baked in here.
#
# The image carries dataset_public/ (synthetic) and nothing else. Your real ledger is not in it, is
# not copied by it, and in demo mode cannot be opened by it — see statement_agent/web/demo.py.

FROM python:3.12-slim

# pdf/image reading needs a couple of system libraries; nothing else is installed
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY statement_agent/ ./statement_agent/
COPY dataset_public/ ./dataset_public/

# Run as a non-root user: a parser bug should not be a root shell.
RUN useradd --create-home --uid 10001 agent \
 && mkdir -p /data \
 && chown -R agent:agent /app /data
USER agent

# Sandboxes and the demo seed live here. Mount a volume at /data if you want them to survive a
# restart; for a demo they are meant to be disposable, so not mounting one is a valid choice.
ENV STATEMENT_AGENT_DEMO_ROOT=/data/demo \
    STATEMENT_AGENT_MODE=demo \
    PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8080)}/healthz').read()"

# Build the synthetic demo seed at boot, then serve. The seed build is cheap and keeps the image
# free of a prebuilt database. Two workers with a long timeout: reading a large PDF is slow.
CMD ["sh", "-c", "python -m statement_agent.cli build-demo-seed --source dataset_public || true; \
     exec gunicorn 'statement_agent.web.app:create_app()' \
       --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 180 \
       --access-logfile - --error-logfile -"]
