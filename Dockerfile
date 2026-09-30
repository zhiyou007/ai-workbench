# ai-workbench · Agnes 免费 AI 工作台镜像
# 构建：docker build -t ai-workbench .
FROM python:3.11-slim

# ffmpeg：长视频接力（抽尾帧 + 分段拼接）依赖，系统 PATH 即可被自动探测
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py index.html start.bat ./
RUN mkdir -p data/output data/videos tools

# config.json（含 API key）与 data/（生成资产与队列）由运行时挂载，不入镜像
EXPOSE 8010

CMD ["python", "app.py"]
