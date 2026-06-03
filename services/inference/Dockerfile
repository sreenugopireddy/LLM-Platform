FROM python:3.12-slim AS builder
WORKDIR /build
COPY services/inference/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app
COPY --from=builder /install /usr/local
COPY services/inference/ /app/services/inference/
COPY shared/ /app/shared/
RUN touch /app/__init__.py /app/services/__init__.py

ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
USER app
EXPOSE 8001
CMD ["uvicorn", "services.inference.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2", "--timeout-graceful-shutdown", "30"]