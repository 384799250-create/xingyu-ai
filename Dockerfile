FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 libsndfile1 && rm -rf /var/lib/apt/lists/*
WORKDIR /service
COPY requirements.txt /service/requirements.txt
RUN pip install --no-cache-dir -r /service/requirements.txt
COPY production/ /service/
CMD ["python", "companion_app.py"]
