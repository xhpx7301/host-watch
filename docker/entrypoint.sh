#!/bin/sh
set -eu
python docker/bootstrap.py
exec "$@"
