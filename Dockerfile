FROM python:3.11-slim

WORKDIR /app

# Install deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Railway provides $PORT
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
