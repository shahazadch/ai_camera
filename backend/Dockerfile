FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgomp1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake YOLOv4-tiny into the image so detection works offline on first start.
# The model_cache volume is seeded from here when it is first created.
RUN mkdir -p /app/model_cache/yolov4_tiny && cd /app/model_cache/yolov4_tiny \
    && python -c "import urllib.request as u; \
[u.urlretrieve(url, name) for url, name in [\
('https://raw.githubusercontent.com/AlexeyAB/darknet/master/cfg/yolov4-tiny.cfg', 'yolov4-tiny.cfg'), \
('https://github.com/AlexeyAB/darknet/releases/download/yolov4/yolov4-tiny.weights', 'yolov4-tiny.weights'), \
('https://raw.githubusercontent.com/AlexeyAB/darknet/master/data/coco.names', 'coco.names')]]"

ENV OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp|stimeout;8000000|rw_timeout;8000000|max_delay;500000

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py seed_dev_data && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 4 --timeout 120"]
