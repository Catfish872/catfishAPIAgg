# 1. Base image
FROM python:3.11-slim

# 2. Set working directory
WORKDIR /app

# 3. Copy requirements
COPY requirements.txt .

# 4. Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy static files (frontend)
COPY static ./static

# 6. Copy application code
COPY main.py .

# 7. Create and declare data volume
# This is where config.json and stats.json will be stored
RUN mkdir -p /app/data
VOLUME /app/data

# 8. Expose port (will be read from $PORT env, default 8080)
# We expose 8080 as a fallback default, but the running port
# will be determined by the $PORT environment variable.
EXPOSE 8080

# 9. Start command
# Run main.py directly, which will read $PORT from the environment
CMD ["python", "main.py"]