FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/state \
    LOG_DIR=/app/logs \
    INDEX_DIR=/app/faiss_index

WORKDIR /app

# Dependencies first, so this layer is cached between code changes.
COPY requirements/backend.txt requirements/backend.txt
RUN pip install --no-cache-dir -r requirements/backend.txt

# Bake tiktoken's encoding into the image so containers never download it at run time.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"

COPY config.py team.py ./
COPY agents ./agents
COPY core ./core
COPY backend ./backend
COPY mcp_servers ./mcp_servers
COPY knowledge_base ./knowledge_base
COPY data/catalogue.json data/orders.json ./data/
COPY storefront ./storefront

# Run as an unprivileged user; data, logs and the FAISS index live on volumes.
RUN useradd --create-home --uid 1000 app && mkdir -p /app/state /app/logs /app/faiss_index && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health', timeout=4)"

# --proxy-headers: trust X-Forwarded-For from the reverse proxy so rate limits see the visitor's IP.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
