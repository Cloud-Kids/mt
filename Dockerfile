# MediTrack — Flask application image (LKS 2027 City Level)
# Participants build and publish this image to their ECR repository.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source
COPY . .

# Run as a non-root user.
RUN useradd -m appuser
USER appuser

EXPOSE 8080

# Initialize the schema at startup, then run two Gunicorn workers on port 8080.
CMD ["sh", "-c", "python -c 'import app; app.init_schema()' || true; gunicorn -w 2 -b 0.0.0.0:8080 app:app"]
