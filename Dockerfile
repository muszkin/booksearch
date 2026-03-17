FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask gunicorn beautifulsoup4 lxml
COPY app.py .
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "2", "--threads", "4", "--timeout", "120", "app:app"]
