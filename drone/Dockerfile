FROM python:3.12-slim
WORKDIR /app/drone
COPY drone/requirements.txt .
RUN pip install --no-cache-dir websockets pyyaml pillow
COPY drone /app/drone
CMD ["python", "agent.py"]
