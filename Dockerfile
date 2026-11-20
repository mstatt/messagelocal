FROM python:3.12-slim
RUN pip install --no-cache-dir "cryptography>=42" "zstandard>=0.22"
WORKDIR /app
COPY app/ /app/
ENV DATA_DIR=/data PORT=9950 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 9950
CMD ["python", "server.py"]
