FROM python:3.11-slim
RUN apt-get update -qq && apt-get install -y --no-install-recommends shellcheck shfmt python3-tk xvfb xauth libtk8.6 imagemagick cmake build-essential git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY . .
CMD ["sh", "tests/with-xvfb.sh", "sh", "tests/check.sh"]
