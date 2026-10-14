FROM python:3.12-slim-trixie
COPY worker.py /worker.py
USER nobody
CMD ["python", "-u", "/worker.py"]
