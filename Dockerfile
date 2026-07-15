FROM python:3.12-slim

# Don't buffer stdout/stderr so logs show up immediately in `docker logs`.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first so this layer is cached unless requirements change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY config.py apify_scraper.py bot.py ./

# Run as a non-root user.
RUN useradd --create-home appuser
USER appuser

CMD ["python", "bot.py"]
