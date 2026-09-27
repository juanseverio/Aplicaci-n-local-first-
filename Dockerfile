FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRESENCIALIDAD_DATA_DIR=/data \
    PRESENCIALIDAD_HOST=0.0.0.0 \
    PORT=8765
COPY requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt
COPY server.py index.html styles.css app.js i18n.js manifest.webmanifest ./
VOLUME ["/data"]
EXPOSE 8765
CMD ["python", "server.py", "--host", "0.0.0.0", "--no-browser"]
