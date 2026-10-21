FROM python:3.12-alpine
RUN pip install --no-cache-dir aiohttp==3.* pymysql==1.*
WORKDIR /app
COPY server.py /app/server.py
CMD ["python", "-u", "/app/server.py"]
