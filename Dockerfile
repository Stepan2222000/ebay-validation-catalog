# 3.13 (а не 3.14): под 3.14 ещё нет колёс torch
FROM python:3.13-slim

WORKDIR /app

# CPU-версия torch (~200 МБ вместо ~2 ГБ с CUDA) — отдельно из CPU-индекса
RUN pip install --no-cache-dir torch==2.12.0 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml .
COPY migrations ./migrations
COPY src ./src
COPY junk_filter ./junk_filter

ENV PYTHONPATH=/app:/app/src \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "validator.main"]
