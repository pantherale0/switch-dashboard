FROM python:3.14-slim

ARG VERSION=2.0.0

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DASHBOARD_DATA_DIR=/data \
    HOME=/data \
    APP_VERSION=${VERSION}

# Metadata labels
LABEL org.opencontainers.image.title="Switch Dashboard" \
      org.opencontainers.image.description="Real-time Network Switch & Device Monitoring Dashboard" \
      org.opencontainers.image.url="https://github.com/pantherale0/switch-dashboard" \
      org.opencontainers.image.source="https://github.com/pantherale0/switch-dashboard" \
      org.opencontainers.image.licenses="MIT"

# Create and set the workspace directory
WORKDIR /app

# Install exactly the reviewed dependency graph.
RUN pip install --no-cache-dir uv==0.11.16
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy the rest of the application files
COPY . .

# Create the data directory
RUN groupadd --gid 10001 switch-dashboard \
    && useradd --uid 10001 --gid switch-dashboard --no-create-home --shell /usr/sbin/nologin switch-dashboard \
    && mkdir -p /data \
    && chown -R switch-dashboard:switch-dashboard /data /app

USER 10001:10001

# Expose Flask port
EXPOSE 8080

# Run the application
CMD ["/app/.venv/bin/gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "60", "app:app"]
