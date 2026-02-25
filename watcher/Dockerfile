FROM python:3.12-alpine

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy watcher script
COPY watcher.py .

# Persisted state directory
VOLUME ["/data"]

CMD ["python", "-u", "watcher.py"]
