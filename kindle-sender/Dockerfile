FROM python:3.12-slim
RUN apt-get update && \
    apt-get install -y --no-install-recommends calibre && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY kindle-sender.py .
CMD ["python", "kindle-sender.py"]
