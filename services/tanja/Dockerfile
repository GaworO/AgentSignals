FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
# Exactly one worker and one Railway replica: SQLite + one background consumer.
# Access logging stays off so the private feed URL is not printed.
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 4 --timeout 30 --bind 0.0.0.0:${PORT:-8080} 'app:create_app()'"]
