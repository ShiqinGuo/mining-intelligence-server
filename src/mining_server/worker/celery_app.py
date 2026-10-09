from mining_server.infrastructure.settings import Settings
from mining_server.worker.broker import create_celery

app = create_celery(Settings())
