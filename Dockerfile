FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (git and sqlite3)
RUN apt-get update && apt-get install -y git sqlite3 && rm -rf /var/lib/apt/lists/*

# Clone the ppxml2db utility directly into the image
RUN git clone https://github.com/vondis/ppxml2db.git .

# Copy your custom python automation scripts into the container
COPY append_transactions.py /app/append_transactions.py
COPY sync_pipeline.sh /app/sync_pipeline.sh

RUN chmod +x /app/sync_pipeline.sh

# CRITICAL: Keep container alive so Dokploy can execute commands inside it
CMD ["tail", "-f", "/dev/null"]