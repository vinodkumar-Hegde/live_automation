FROM python:3.12-slim
WORKDIR /srv/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY office_recorder ./office_recorder
COPY ["Start Office Recorder.command", "requirements-office.txt", "RECORDING_SETUP.md", "./"]
RUN mkdir -p /srv/app/data
EXPOSE 8000
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--proxy-headers","--no-access-log"]
