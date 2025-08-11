FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app

VOLUME ["/data"]
ENV DB_PATH=/data/astrobot.sqlite

CMD ["python", "-m", "app.bot"]