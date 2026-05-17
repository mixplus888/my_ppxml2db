FROM python:3.12-slim

WORKDIR /app

# Install sqlite3 and compiler tools needed for lxml compilation
RUN apt-get update && apt-get install -y sqlite3 libxml2-dev libxslt-dev gcc && rm -rf /var/lib/apt/lists/*

# Force install lxml library
RUN pip install --no-cache-dir lxml

# Copy all your repository files into the container
COPY . /app

RUN chmod +x /app/sync_pipeline.sh

# Keep container alive for execution
CMD ["tail", "-f", "/dev/null"]