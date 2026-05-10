FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (Docker layer caching — rebuilds are faster)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code after deps (code changes don't bust the dep cache layer)
COPY main.py .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]