FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-tk tk xvfb xauth x11-utils imagemagick \
        libportaudio2 fonts-dejavu fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pillow numpy sounddevice
WORKDIR /app
