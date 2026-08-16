FROM python:3.12-alpine
WORKDIR /app
COPY drift.py .
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=60s --timeout=5s CMD pgrep -f drift.py || exit 1
ENTRYPOINT ["python3", "drift.py"]
