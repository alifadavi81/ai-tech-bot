# syntax=docker/dockerfile:1
FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata \
  && rm -rf /var/lib/apt/lists/*

# (Optional) create a non-root user
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Install deps first for caching
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Ensure runtime dirs exist & perms ok
RUN mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 TZ=${TZ:-Europe/Berlin}

# Expose for compose/reverse proxy (PORT env controls runtime)
EXPOSE 10000

CMD ["python", "bot.py"]
