FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py tariffs.yaml ./
RUN useradd -r app && mkdir -p /bills && chown app /bills
USER app
