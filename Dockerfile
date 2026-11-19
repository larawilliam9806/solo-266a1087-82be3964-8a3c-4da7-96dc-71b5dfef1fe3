FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py ./

ENV APP_DATABASE=/data/app.db \
    PORT=8000
VOLUME /data
EXPOSE 8000

CMD ["python", "run.py"]
