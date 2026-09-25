FROM python:3.11-slim

WORKDIR /app

# 仅主程序依赖；Cookie 抓取（cloakbrowser）需要桌面浏览器，容器内不提供
RUN pip install --no-cache-dir \
    fastapi>=0.110 \
    "uvicorn[standard]>=0.29" \
    pydantic>=2 \
    curl_cffi>=0.7

COPY genspark2api.py ./
COPY static/ ./static/

# 数据目录：accounts.json / config.json / cookies*.json / logs 全部落在这里，挂卷即持久化
ENV GS_DATA_DIR=/data \
    GS_HOST=0.0.0.0 \
    GS_PORT=8899
VOLUME /data
RUN mkdir -p /data

EXPOSE 8899

CMD ["python", "genspark2api.py"]
