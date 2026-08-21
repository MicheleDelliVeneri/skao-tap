FROM python:3.12-slim
WORKDIR /srv
COPY libs/tapcore ./libs/tapcore
RUN pip install --no-cache-dir ./libs/tapcore
COPY services/tap-executor/executor ./executor
CMD ["python", "-m", "executor.worker"]
