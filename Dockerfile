FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-fly.txt .
RUN pip3 install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu \
      torch==2.7.1 \
    && pip3 install --no-cache-dir -r requirements-fly.txt

COPY . .

RUN mkdir -p /opt/asklit-seed-data \
    && cp -a /app/data/. /opt/asklit-seed-data/ \
    && chmod +x /app/scripts/start-container.sh

EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health

ENTRYPOINT ["/app/scripts/start-container.sh"]
