FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    calibre inotify-tools file \
    && rm -rf /var/lib/apt/lists/*
COPY import.sh /app/import.sh
RUN chmod +x /app/import.sh
CMD ["/app/import.sh"]
