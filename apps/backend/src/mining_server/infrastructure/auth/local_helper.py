import argparse
import asyncio
import subprocess
import sys
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import parse_qs, urlsplit

import httpx
from mining_contracts.domain.core import ErrorCode
from pydantic import SecretStr

from mining_server.domain.auth import (
    InstallationView,
    OAuthCallback,
    OAuthLimits,
    RegistrationImport,
    SecretSerialization,
    utcnow,
)
from mining_server.domain.core import DomainError, fail
from mining_server.infrastructure.auth.oauth import (
    OAuthClient,
    authorization_url,
    create_attempt,
)


class CallbackParameters(TypedDict, total=False):
    state: list[str]
    code: list[str]
    client_id: list[str]
    error: list[str]
    scope: list[str]


async def pair(installation: InstallationView, command: list[str]) -> None:
    if utcnow() >= installation.expires_at:
        raise fail(ErrorCode.WAITING_AUTH, "Installation ticket expired")
    result: asyncio.Future[OAuthCallback] = asyncio.get_running_loop().create_future()
    expected_state = ""
    limits = OAuthLimits()

    async def callback(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=limits.callback_read_seconds
            )
            first_line = request.split(b"\r\n", 1)[0].decode("ascii")
            method, target, version = first_line.split(" ")
            parsed = urlsplit(target)
            values = cast(
                CallbackParameters, parse_qs(parsed.query, strict_parsing=True)
            )
            if (
                method != "GET"
                or version != "HTTP/1.1"
                or parsed.path != "/auth/callback"
                or parsed.scheme
                or parsed.netloc
            ):
                raise ValueError("Invalid callback target")
            if any(len(value) != 1 for value in values.values()) or (
                "state" not in values or values["state"] != [expected_state]
            ):
                raise ValueError("Invalid callback state")
            if set(values) - {"state", "code", "client_id", "error", "scope"}:
                raise ValueError("Unknown OAuth callback parameter")
            data = OAuthCallback(
                state=values["state"][0],
                code=SecretStr(values["code"][0]) if "code" in values else None,
                client_id=values["client_id"][0] if "client_id" in values else None,
                error=values["error"][0] if "error" in values else None,
                scope=values["scope"][0] if "scope" in values else None,
            )
            if result.done():
                raise ValueError("Callback already consumed")
            result.set_result(data)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nAuthorization received. Return to the pairing terminal."
            )
        except (
            TimeoutError,
            ValueError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            writer.write(
                b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nInvalid callback."
            )
        finally:
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(
        callback, "127.0.0.1", 0, limit=limits.callback_header_bytes
    )
    try:
        port = server.sockets[0].getsockname()[1]
        attempt = create_attempt(
            installation.app_name,
            installation.target_host_id,
            f"http://127.0.0.1:{port}/auth/callback",
            installation.client_id,
            installation.expected_subject,
        )
        expected_state = attempt.state.get_secret_value()
        print("Open this URL to continue with ChatGPT:", flush=True)
        print(authorization_url(attempt), flush=True)
        response = await asyncio.wait_for(
            result,
            timeout=min(
                limits.attempt_seconds,
                (installation.expires_at - utcnow()).total_seconds(),
            ),
        )
        async with httpx.AsyncClient(
            timeout=limits.http_timeout_seconds, trust_env=False
        ) as client:
            credentials = await OAuthClient(client).exchange(attempt, response)
        registration = RegistrationImport(
            installation_id=installation.installation_id,
            target_host_id=installation.target_host_id,
            app_name=installation.app_name,
            credentials=credentials,
        )
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            input=registration.model_dump_json(
                context=SecretSerialization.REVEAL
            ).encode(),
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise fail(
                ErrorCode.WAITING_AUTH,
                "Credential handoff failed; backend import did not confirm success",
            )
        print("Backend credential import completed.", flush=True)
    finally:
        server.close()
        await server.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--handoff-command", nargs=argparse.REMAINDER, required=True)
    arguments = parser.parse_args()
    if not arguments.handoff_command:
        parser.error("A Docker or SSH stdin import command is required")
    installation = InstallationView.model_validate_json(
        arguments.installation.read_text(encoding="utf-8")
    )
    asyncio.run(pair(installation, arguments.handoff_command))


if __name__ == "__main__":
    try:
        main()
    except (DomainError, ValueError, OSError, httpx.HTTPError, TimeoutError):
        print("Pairing failed. No credential success was confirmed.", file=sys.stderr)
        raise SystemExit(1) from None
