FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY capture.py .

# Long-running worker: long-polls Telegram, no inbound ports. PYTHONUNBUFFERED
# keeps logs flowing to `docker logs` / `caprover logs` in real time.
ENV PYTHONUNBUFFERED=1

CMD ["python", "capture.py"]
