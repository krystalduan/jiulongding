# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.11.7

FROM python:${PYTHON_VERSION}-slim

LABEL fly_launch_runtime="flask"

WORKDIR /code

COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

COPY . .

EXPOSE 8080

# Single worker on purpose: rate limits and caches are in memory.
# Threads are fine since everything slow is I/O.
# Access log leaves out query strings (the cron secret is passed in one) and
# ends with Fly's request id, which also appears on "FAILED unhandled" lines.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", \
     "--workers", "1", "--threads", "8", \
     "--timeout", "60", "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--access-logformat", "%(t)s \"%(m)s %(U)s\" %(s)s %(b)s %(L)ss request=%({fly-request-id}i)s", \
     "app:app"]
