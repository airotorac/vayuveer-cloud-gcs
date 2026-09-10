FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt
COPY server /app/server
COPY dashboard /app/dashboard
WORKDIR /app/server
ENV VAYUVEER_TOKEN=change-me
ENV VAYUVEER_LOG_DIR=/data/flights
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "4194304"]
