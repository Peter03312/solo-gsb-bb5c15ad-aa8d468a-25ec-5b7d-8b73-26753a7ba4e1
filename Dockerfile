FROM python:3.12-slim

WORKDIR /app
COPY . .

# 应用仅依赖 Python 标准库，无需安装第三方包
ENV HOST=0.0.0.0 \
    PORT=8000 \
    DB_PATH=/data/museum.db

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "app.py"]
