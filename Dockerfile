# TriBrief 运行镜像：轻量、可复现
FROM python:3.11-slim

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝源码与模板
COPY brief.py .
COPY news_pipeline.py .
COPY templates/ ./templates/

# 运行时挂载 config.yaml 与 output/，密钥用 env 注入
ENTRYPOINT ["python", "brief.py"]
