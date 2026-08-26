# AGENT://BREAK — образ для Render / Koyeb / Hugging Face Spaces / Cloud Run / Fly
# (на HF Spaces контейнер обязан слушать порт 7860 — он подставлен по умолчанию;
#  Render и Cloud Run передают свой PORT через переменную окружения — она победит)
FROM python:3.12-slim

WORKDIR /app
COPY server.py requirements.txt ./
RUN mkdir -p data

ENV HOST=0.0.0.0 \
    PORT=7860

EXPOSE 7860
# лёгкий healthcheck: /health отвечает всегда
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','7860')+'/health', timeout=4)" || exit 1

CMD ["python", "server.py"]
