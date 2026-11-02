# Classic Mac OS X Dashboard "dead service" revival server.
# Pure Python 3 standard library (no pip packages). The vintage widgets are
# plain HTTP and hit port 80, so the container MUST be published on host port
# 80 (an /etc/hosts entry cannot carry a port number).
FROM python:3.12-slim

LABEL org.opencontainers.image.title="dashboard_fix" \
      org.opencontainers.image.description="Revives Mac OS X 10.4-10.14 Dashboard widgets (weather/stocks/currency/translation)" \
      org.opencontainers.image.source="https://github.com/bugprogrammer/classic-macos-dashboard-revival"

WORKDIR /app

# Only the program and the bundled WOEID table are required. geo_cache.json is
# written here at runtime (an atomic .tmp+rename); tests and README are not
# needed inside the image.
COPY dashboard_fix.py zipcodes.json ./

# Widgets talk plain HTTP on 80.
EXPOSE 80

# No curl in the slim image -> use Python for the health probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=8s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1/health', timeout=5).status==200 else 1)"

# -u for unbuffered logs so `docker logs` shows every request live.
CMD ["python3", "-u", "dashboard_fix.py"]