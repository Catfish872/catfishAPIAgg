# 1. Base image
FROM python:3.11-slim

# 2. Set working directory
WORKDIR /app

# 3. Python runtime settings
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 4. Copy requirements first for Docker layer cache
COPY requirements.txt .

# 5. Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# 6. Copy application source code
# This includes main.py, endpoint_presets.py, static/, and other future modules.
COPY . .

# 7. Ensure data directory exists
# Runtime config.json and stats.json are stored here.
RUN mkdir -p /app/data

# 8. Declare data volume
VOLUME /app/data

# 9. Expose default port
# The app should still read $PORT at runtime.
EXPOSE 8080

# 10. Start command
CMD ["python", "main.py"]
