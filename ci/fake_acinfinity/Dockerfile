FROM python:3.12-alpine
WORKDIR /srv
# Same base and hardening as the app image (security updates, no pip at runtime).
RUN apk upgrade --no-cache
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y pip
COPY server.py .
EXPOSE 9000
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health')" || exit 1
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9000"]
