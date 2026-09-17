FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STUDY_DATA_DIR=/data \
    STUDY_COOKIE_SECURE=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY study ./study
COPY web ./web
COPY builtin_books ./builtin_books

EXPOSE 8080
CMD ["sh", "-c", "exec gunicorn 'study.app:create_app()' --bind 0.0.0.0:${PORT:-8080} --workers 1 --worker-class gthread --threads 8 --timeout 180 --graceful-timeout 30 --access-logfile - --error-logfile -"]
