FROM nginx:1.27-alpine AS frontend
COPY frontend/index.html frontend/styles.css frontend/app.js /usr/share/nginx/html/

FROM python:3.11-slim AS backend
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    PROJECT_ROOT=/project MLFLOW_TRACKING_URI=sqlite:////project/runtime/mlflow.db
RUN apt-get update && apt-get install -y --no-install-recommends git libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*
# The bind-mounted checkout belongs to the host user; container commands run as root.
# Trust only this project path so Git provenance checks work inside the image.
RUN git config --system --add safe.directory /project
WORKDIR /project
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY *.py dvc.yaml params.yaml MLproject ./
COPY tests/ tests/
COPY frontend/ frontend/
COPY monitoring/ monitoring/
COPY compose.yaml Dockerfile .gitignore .dockerignore ./
COPY data/banking77.zip data/
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
