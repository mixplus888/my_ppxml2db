FROM python:3.12-slim

WORKDIR /app

# Install sqlite3 for the database conversions
RUN apt-get update && apt-get install -y sqlite3 && rm -rf /var/lib/apt/lists/*

# Install python dependencies from your repo
COPY requirements.txt* ./
RUN pip install --no-cache-dir -r requirements.txt || echo "No requirements file or empty"

# Copy all your repository files (including ppxml2db.py) into the container
COPY . /app

RUN chmod +x /app/sync_pipeline.sh

# Keep container alive for execution
CMD ["tail", "-f", "/dev/null"]