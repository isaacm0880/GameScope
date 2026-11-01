FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 botuser
COPY *.py ./
RUN mkdir -p /data && chown -R botuser:botuser /data
USER botuser
CMD ["python", "-u", "bot.py"]
