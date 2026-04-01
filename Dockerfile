FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

WORKDIR /app

# Install Node.js for OpenClaw gateway
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install OpenClaw
RUN npm install -g @openclaw/cli

# Install Python deps (for MCP server)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Copy OpenClaw config
RUN mkdir -p /root/.openclaw && \
    cp openclaw.config.json /root/.openclaw/config.json && \
    cp SOUL.md /root/.openclaw/SOUL.md

CMD ["openclaw", "gateway", "start"]
