FROM python:3.12-slim

# git + Node.js 20 + Claude Code CLI
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && rm -rf /var/lib/apt/lists/*

# run as a non-root user
RUN useradd -m app
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
RUN chown -R app:app /app
USER app

# the repo working copy lives here; it is wiped whenever the container is recreated
ENV WORKDIR=/tmp/rag-project-work
ENV PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
