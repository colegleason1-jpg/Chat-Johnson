# syntax=docker/dockerfile:1
# One image for the app and the worker; WITH_BROWSER=1 adds Chromium for browser checks in the worker.
FROM python:3.12-slim AS base
ARG WITH_BROWSER=0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
WORKDIR /app
RUN groupadd --system app && useradd --system --gid app --create-home app
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt \
 && if [ "$WITH_BROWSER" = "1" ]; then pip install playwright && playwright install --with-deps chromium; fi
COPY . .
RUN mkdir -p /data /opt/pw-browsers && chown -R app:app /app /data /opt/pw-browsers
USER app
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4).status < 400 else 1)"
CMD ["python", "-m", "streamlit", "run", "app.py", "--server.port", "8501", "--server.address", "0.0.0.0"]
