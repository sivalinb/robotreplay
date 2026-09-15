FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RR_DATA_DIR=/var/lib/robotreplay
WORKDIR /app
COPY requirements.lock pyproject.toml ./
COPY robotreplay ./robotreplay
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home replay \
    && mkdir -p /var/lib/robotreplay && chown -R replay:replay /var/lib/robotreplay
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["python", "-m", "uvicorn", "robotreplay.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
