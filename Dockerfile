FROM python:3.12-slim-bookworm

# Chrome + deps para Selenium headless
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg unzip curl ca-certificates \
    fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 \
    libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 \
    libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 xdg-utils \
    && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y /tmp/chrome.deb || apt-get install -fy \
    && rm -rf /var/lib/apt/lists/* /tmp/chrome.deb

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV SITRAX_HEADLESS=true
ENV PORT=8000
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Railway injeta $PORT — run.py lê PORT do ambiente
CMD ["python", "run.py", "serve", "--host", "0.0.0.0"]
