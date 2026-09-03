FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Headless Chromium for the campaign PDF renderer (/export-pdf).
# --with-deps pulls the system libraries Chromium needs on Debian slim; without
# it the browser installs but fails at launch on a missing shared object, which
# is exactly how the old Netlify function died (libnspr4.so).
RUN playwright install --with-deps chromium

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
