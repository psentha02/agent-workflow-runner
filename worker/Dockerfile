FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .

# No EXPOSE, no CMD with a server — this is a run-to-completion container
CMD ["python", "agent.py"]