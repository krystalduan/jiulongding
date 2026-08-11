# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.11.7

FROM python:${PYTHON_VERSION}-slim

LABEL fly_launch_runtime="flask"

WORKDIR /code

COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

COPY . .

EXPOSE 8080

# One worker, several threads — deliberately, not for lack of capacity.
#
# Rate limiting and the sheets connection cache live in process memory, so
# with 2 workers each request hit whichever worker load balancing chose and
# the effective limits were double the configured numbers. A single worker
# makes them exact.
#
# Throughput does not suffer: fly.toml allocates 1 CPU, so 2 processes gave
# no real parallelism, and every slow path here is I/O (Sheets, Resend, SMS)
# which threads handle fine. --timeout restarts a worker wedged on a hung
# upstream call.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", \
     "--workers", "1", "--threads", "8", \
     "--timeout", "60", "--graceful-timeout", "30", \
     "--access-logfile", "-", "app:app"]
