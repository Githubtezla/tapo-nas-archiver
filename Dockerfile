FROM python:3.13-slim-bookworm

# ffmpeg turns the stream from the device into MP4 files
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV PYTHONUNBUFFERED=1
WORKDIR /app
CMD ["python", "/app/tapo_archiver.py", "run"]
