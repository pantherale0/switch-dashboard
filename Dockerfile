FROM python:3.14-slim

ARG VERSION=2.0.0

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DASHBOARD_DATA_DIR=/data \
    APP_VERSION=${VERSION}

# Metadata labels
LABEL org.opencontainers.image.title="Switch Dashboard" \
      org.opencontainers.image.description="Real-time Network Switch & Device Monitoring Dashboard" \
      org.opencontainers.image.url="https://github.com/pantherale0/switch-dashboard" \
      org.opencontainers.image.source="https://github.com/pantherale0/switch-dashboard" \
      org.opencontainers.image.licenses="MIT"

# Create and set the workspace directory
WORKDIR /app

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . .

# Create the data directory
RUN mkdir -p /data

# Expose Flask port
EXPOSE 8080

# Run the application
CMD ["python", "app.py"]
