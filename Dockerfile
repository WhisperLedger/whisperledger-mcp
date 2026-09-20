FROM python:3.12-slim

WORKDIR /app

# Install git and gh cli if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

EXPOSE 5005

CMD ["python", "api_server.py"]
