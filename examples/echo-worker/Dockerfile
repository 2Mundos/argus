FROM python:3.12-slim-trixie
LABEL argus.worker="plain"
COPY worker.py /worker.py
USER 65534:65534
CMD ["python", "-u", "/worker.py"]
