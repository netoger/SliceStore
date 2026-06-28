# service/Dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY service/ .
COPY format/ ./format/

ENV PYTHONPATH=/app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
