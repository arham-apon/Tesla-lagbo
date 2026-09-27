# Generic service Dockerfile from the plan (Part 8.1). Build context = repo root.
FROM python:3.12-slim
ARG SERVICE=trip
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY libs/common /libs/common
COPY services/${SERVICE}/requirements.txt .
RUN pip install --no-cache-dir /libs/common -r requirements.txt
COPY services/${SERVICE}/ .
RUN mkdir -p /data
EXPOSE 8003
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8003"]
