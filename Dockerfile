FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
# Root services run the main Flask app. Tanja has services/tanja/Dockerfile.
# A shell is required to expand Railway's PORT environment variable.
CMD ["sh", "-c", "exec gunicorn agent:app --bind 0.0.0.0:${PORT:-8080}"]
