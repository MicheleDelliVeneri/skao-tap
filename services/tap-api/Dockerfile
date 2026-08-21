FROM python:3.12-slim
WORKDIR /srv
COPY libs/tapcore ./libs/tapcore
COPY services/tap-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt ./libs/tapcore
COPY services/tap-api/app ./app
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
