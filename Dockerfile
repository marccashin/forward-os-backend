# Pinned to bookworm (Debian 12) deliberately.
#
# The unpinned python:3.11-slim tag now resolves to Debian 13 (trixie).
# Playwright 1.47 carries a hardcoded system-package list for Debian 11 and 12;
# several of those packages were renamed in trixie, so `playwright install
# --with-deps` fails at apt with exit code 100 before it downloads anything.
# Do not drop the -bookworm suffix without also moving Playwright to a version
# that knows the newer distro.
FROM python:3.11-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Headless Chromium for the campaign PDF renderer (/export-pdf).
RUN playwright install --with-deps chromium

# Fail the BUILD if Chromium cannot actually launch, rather than shipping an
# image that only reveals the problem when an agent exports a campaign.
RUN python -c "\
from playwright.sync_api import sync_playwright; \
p = sync_playwright().start(); \
b = p.chromium.launch(args=['--no-sandbox','--disable-dev-shm-usage']); \
print('chromium ok:', b.version); \
b.close(); p.stop()"

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
