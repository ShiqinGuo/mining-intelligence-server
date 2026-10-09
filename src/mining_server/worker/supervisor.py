import asyncio
import signal
import sys
from dataclasses import dataclass
from enum import StrEnum

from mining_server.infrastructure.settings import Settings


class ProcessRole(StrEnum):
    API = "api"
    WORKER = "worker"
    BEAT = "beat"


@dataclass(frozen=True)
class ProcessCommand:
    role: ProcessRole
    arguments: tuple[str, ...]


async def run() -> None:
    settings = Settings()
    commands = (
        ProcessCommand(
            ProcessRole.API,
            (
                sys.executable,
                "-m",
                "uvicorn",
                "mining_server.api.app:create_app",
                "--factory",
                "--host",
                settings.api_host,
                "--port",
                str(settings.api_port),
            ),
        ),
        ProcessCommand(
            ProcessRole.WORKER,
            (
                sys.executable,
                "-m",
                "celery",
                "-A",
                "mining_server.worker.celery_app:app",
                "worker",
                "--loglevel=INFO",
                "--pool=prefork",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                f"--concurrency={settings.worker_concurrency}",
            ),
        ),
        ProcessCommand(
            ProcessRole.BEAT,
            (
                sys.executable,
                "-m",
                "celery",
                "-A",
                "mining_server.worker.celery_app:app",
                "beat",
                "--loglevel=INFO",
                "--schedule",
                str(settings.data_dir / "celerybeat-schedule"),
            ),
        ),
    )
    await asyncio.to_thread(settings.data_dir.mkdir, parents=True, exist_ok=True)
    processes = [
        await asyncio.create_subprocess_exec(*command.arguments) for command in commands
    ]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    waits = [asyncio.create_task(process.wait()) for process in processes]
    stopping = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait(
        [*waits, stopping], return_when=asyncio.FIRST_COMPLETED
    )
    unexpected = stopping not in done
    for process in processes:
        if process.returncode is None:
            process.terminate()
    try:
        await asyncio.wait_for(
            asyncio.gather(*waits), timeout=settings.process_shutdown_seconds
        )
    except TimeoutError:
        for process in processes:
            if process.returncode is None:
                process.kill()
        await asyncio.gather(*waits)
    stopping.cancel()
    await asyncio.gather(stopping, return_exceptions=True)
    if unexpected:
        raise SystemExit("A managed process exited unexpectedly")


if __name__ == "__main__":
    asyncio.run(run())
