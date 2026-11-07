FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py .
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 4 --timeout 20 --bind 0.0.0.0:${PORT:-8080} 'relay:create_app()'"]
