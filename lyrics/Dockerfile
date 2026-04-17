
# syntax=docker/dockerfile:1.4
FROM --platform=$BUILDPLATFORM python:3.14.4-alpine3.23 AS builder

WORKDIR /src
COPY requirements.txt /src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip3 install -r requirements.txt

COPY . .

#CMD ["python3", "api/app.py"]
COPY entrypoint.sh .
RUN chmod uo+x entrypoint.sh

# Create a group and user
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
# Tell docker that all future commands should run as the appuser user
USER appuser
ENTRYPOINT ["/src/entrypoint.sh"]

