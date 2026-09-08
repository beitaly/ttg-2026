FROM python:3.11-slim

# Install base tools needed by playwright install-deps
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

# Run both scripts simultaneously
CMD python ttg_bhi.py & python ttg_bdp.py & wait
