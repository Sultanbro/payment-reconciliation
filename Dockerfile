FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home app && mkdir data && chown app:app data
COPY reconcile ./reconcile
USER app
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["uvicorn", "reconcile.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
