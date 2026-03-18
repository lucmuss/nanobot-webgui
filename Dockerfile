FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Install Node.js 20 for the WhatsApp bridge
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p nanobot_webgui && touch nanobot_webgui/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf nanobot_webgui

# Copy the full source and install
COPY nanobot_webgui/ nanobot_webgui/
RUN uv pip install --system --no-cache .

# Build the WhatsApp bridge shipped by upstream nanobot
WORKDIR /usr/local/lib/python3.12/site-packages/nanobot/bridge
RUN npm install && npm run build
WORKDIR /app

# Create config directory
RUN mkdir -p /root/.nanobot

# Default gateway and GUI ports
EXPOSE 18790 18791

ENTRYPOINT ["nanobot-webgui"]
CMD ["status"]
