#!/bin/sh
if [ "${APP_ENV}" = "prod" ]; then
  echo "Starting in prod ..."
  exec python3 genius.py

else
  echo "Starting not in prod ..."
  exec python3 genius.py
fi

