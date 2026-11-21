FROM python:3.12-slim
# ffmpeg is only needed in "video" mode: --build-arg WITH_FFMPEG=0 for a lighter image
ARG WITH_FFMPEG=1
RUN if [ "$WITH_FFMPEG" = "1" ]; then apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*; fi
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m -u 1000 app && mkdir -p /data && chown -R app:app /data /app
USER app
ENV DATA_DIR=/data PORT=7860 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','7860'), timeout=4)" || exit 1
STOPSIGNAL SIGTERM
CMD ["python", "panel.py"]
