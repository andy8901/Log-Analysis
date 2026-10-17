# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Install system dependencies required for compiling some MAVLink/Python
# extensions, plus rclone -- only used if DRIVE_SYNC_RCLONE_REMOTE is set
# (see app/drive_sync.py), harmless and inert otherwise. Google Drive for
# Desktop (the Windows/Mac Drive-sync mechanism) has no Linux build, so
# this is the one that actually works in a container.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    unzip \
    && curl -fsSL https://rclone.org/install.sh | bash \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy requirements first to leverage Docker caching layers
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Create the dynamic directories required by the app (app/__init__.py also
# creates these itself at startup with exist_ok=True, so this is just a head
# start). instance/ holds the SQLite DB by default; archive/ holds every
# uploaded log, organised per department/drone; uploads/ is just a scratch
# area for files mid-upload.
RUN mkdir -p instance archive uploads

# Default port; most PaaS hosts (Render, Railway, ...) override this via $PORT
ENV PORT=8080
EXPOSE 8080

# Run the app using Gunicorn (production-grade server)
# --workers 2: splits work to process concurrent requests
# --timeout 120: gives pymavlink up to 2 minutes to parse massive logs
# Shell form so ${PORT} (set by most PaaS hosts) is honored at container start.
CMD gunicorn --bind 0.0.0.0:${PORT} --workers 2 --timeout 120 wsgi:app
