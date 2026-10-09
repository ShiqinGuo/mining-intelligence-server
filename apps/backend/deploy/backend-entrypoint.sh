set -eu
alembic upgrade head
exec python -m mining_server.worker.supervisor
