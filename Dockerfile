# Render 배포와 EC2 이전에 같은 이미지를 쓴다.
FROM python:3.12-slim

WORKDIR /app

# git: 인덱싱이 레포를 클론해 `git log -L`로 줄 범위 이력을 읽는다 (#27).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render는 $PORT를 주입한다. 로컬·EC2에서는 8000을 쓴다.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${PORT}"]
