FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Run as non-root user
RUN useradd -r -s /bin/false appuser
USER appuser

# Expose port
EXPOSE 5555

# Run the application
CMD ["python", "app.py"]
