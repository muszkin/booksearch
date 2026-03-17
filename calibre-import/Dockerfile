FROM python:3.12-alpine
RUN apk add --no-cache calibre inotify-tools file bash
COPY import.sh /app/import.sh
RUN chmod +x /app/import.sh
CMD ["/app/import.sh"]
