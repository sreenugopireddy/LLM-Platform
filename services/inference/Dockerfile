FROM python:3.12-slim
WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app
COPY services/inference/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY services/inference/ ./services/inference/
COPY shared/ ./shared/
RUN touch __init__.py services/__init__.py
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
USER app
EXPOSE 8001
CMD ["python", "-m", "uvicorn", "services.inference.main:app", "--host", "0.0.0.0", "--port", "8001"]