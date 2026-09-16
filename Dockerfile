FROM python:3.12-slim
WORKDIR /app
# PDF 出力用の日本語フォント（IPAex ゴシック）
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-ipaexfont-gothic \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["gunicorn", "-b", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "app:app"]
