FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

COPY syno_telegram_gifs /syno_telegram_gifs

# relative data_folder/ffmpeg_working_folder from the config ("/data", "/gifs")
# resolve inside the mounted volumes from here.
WORKDIR /

VOLUME ["/config", "/gifs", "/data"]

ENTRYPOINT ["python3", "-m", "syno_telegram_gifs"]
CMD ["/config/config.json"]