"""Language-neutral client for the local codashe-omni gateway."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from pathlib import PurePosixPath
from typing import Any, Callable, Iterator
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from cli_tools import __version__

JsonObject = dict[str, Any]
WebSocketFactory = Callable[..., Any]
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class CodasheError(RuntimeError):
    """A stable gateway or client failure suitable for CLI diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "client_error",
        retryable: bool = False,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details


class CodasheGatewayClient:
    """Synchronous client for short-lived commands and resumable event streams."""

    def __init__(
        self,
        gateway_url: str,
        *,
        timeout: float = 15.0,
        reconnect_delay: float = 0.25,
        http_client: httpx.Client | None = None,
        websocket_factory: WebSocketFactory = connect,
    ) -> None:
        parts = urlsplit(gateway_url)
        if parts.scheme not in {"ws", "wss"} or not parts.netloc:
            raise ValueError("gateway URL must be an absolute ws:// or wss:// URL")
        self.gateway_url = gateway_url
        self.timeout = timeout
        self.reconnect_delay = reconnect_delay
        self._websocket_factory = websocket_factory
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        http_scheme = "https" if parts.scheme == "wss" else "http"
        self.http_base = urlunsplit((http_scheme, parts.netloc, "", "", ""))
        self._protocol_version: str | None = None

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> "CodasheGatewayClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def health(self) -> JsonObject:
        response = self._http.get(f"{self.http_base}/health")
        response.raise_for_status()
        payload = _object(response.json(), "gateway health response")
        version = payload.get("protocolVersion")
        if not isinstance(version, str) or not version:
            raise CodasheError("gateway health response has no protocol version")
        self._protocol_version = version
        return payload

    def schema(self) -> JsonObject:
        response = self._http.get(f"{self.http_base}/api/protocol/schema.json")
        response.raise_for_status()
        return _object(response.json(), "protocol schema")

    def job_file(self, job_id: str, relative_path: str) -> bytes:
        path = PurePosixPath(relative_path)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("job file path must be a non-empty relative path")
        encoded_path = "/".join(quote(part, safe="") for part in path.parts)
        response = self._http.get(
            f"{self.http_base}/api/jobs/{quote(job_id, safe='')}/{encoded_path}"
        )
        response.raise_for_status()
        return response.content

    def manifest(self, job_id: str) -> JsonObject:
        return _object(json.loads(self.job_file(job_id, "manifest.json")), "manifest")

    def submit(
        self,
        submission: JsonObject,
        *,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        return self.request(
            "job.submit",
            mutation=True,
            idempotency_key=idempotency_key,
            submission=submission,
        )

    def snapshot(self, job_id: str) -> JsonObject:
        response = self.request("job.snapshot", jobId=job_id, raw=True)
        snapshot = response.get("snapshot")
        return _object(snapshot, "job snapshot")

    def control(
        self,
        job_id: str,
        action: str,
        *,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        return self.request(
            "job.control",
            mutation=True,
            idempotency_key=idempotency_key,
            jobId=job_id,
            action=action,
        )

    def retention(
        self,
        job_id: str,
        action: str,
        *,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        return self.request(
            "job.retention",
            mutation=True,
            idempotency_key=idempotency_key,
            jobId=job_id,
            action=action,
        )

    def steer(
        self,
        job_id: str,
        message: str,
        *,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        return self.request(
            "job.steer",
            mutation=True,
            idempotency_key=idempotency_key,
            jobId=job_id,
            message=message,
        )

    def respond(
        self,
        job_id: str,
        request_token: str,
        response: Any,
        *,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        return self.request(
            "human_input.respond",
            mutation=True,
            idempotency_key=idempotency_key,
            jobId=job_id,
            requestToken=request_token,
            response=response,
        )

    def query(self, query: JsonObject) -> JsonObject:
        return self.request("job.query", query=query)

    def request(
        self,
        command_type: str,
        *,
        mutation: bool = False,
        idempotency_key: str | None = None,
        raw: bool = False,
        **fields: Any,
    ) -> JsonObject:
        with self._connection() as (websocket, version):
            request_id = _identifier(command_type)
            command: JsonObject = {
                "type": command_type,
                "protocolVersion": version,
                "requestId": request_id,
                **fields,
            }
            if mutation:
                command["idempotencyKey"] = idempotency_key or _identifier(
                    "operation"
                )
            websocket.send(json.dumps(command, separators=(",", ":")))
            message = self._receive_reply(websocket, request_id)
            if raw:
                return message
            payload = message.get("payload", {})
            return _object(payload, "gateway acknowledgement payload")

    def events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        follow: bool = True,
        idle_timeout: float = 1.0,
        total_timeout: float | None = None,
    ) -> Iterator[JsonObject]:
        cursor = after_sequence
        deadline = time.monotonic() + total_timeout if total_timeout else None
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return
            snapshot = self.snapshot(job_id)
            status = _object(snapshot.get("job"), "snapshot job").get("status")
            latest = snapshot.get("latestSequence")
            if status in TERMINAL_STATUSES and isinstance(latest, int) and cursor >= latest:
                return
            try:
                with self._connection() as (websocket, version):
                    request_id = _identifier("subscribe")
                    websocket.send(
                        json.dumps(
                            {
                                "type": "job.subscribe",
                                "protocolVersion": version,
                                "requestId": request_id,
                                "jobId": job_id,
                                "afterSequence": cursor,
                            },
                            separators=(",", ":"),
                        )
                    )
                    self._receive_reply(websocket, request_id)
                    while True:
                        receive_timeout = _remaining(deadline)
                        if not follow:
                            receive_timeout = min(
                                idle_timeout,
                                receive_timeout if receive_timeout is not None else idle_timeout,
                            )
                        try:
                            message = self._receive(websocket, receive_timeout)
                        except TimeoutError:
                            if not follow or deadline is not None:
                                return
                            continue
                        if message.get("type") == "error":
                            self._raise_protocol_error(message)
                        if message.get("type") != "event":
                            continue
                        event = _object(message.get("event"), "job event")
                        sequence = event.get("sequence")
                        if event.get("jobId") != job_id or not isinstance(sequence, int):
                            continue
                        if sequence <= cursor:
                            continue
                        cursor = sequence
                        yield event
                        if _terminal_event(event):
                            return
            except (ConnectionClosed, OSError):
                if not follow:
                    return
                remaining = _remaining(deadline)
                if remaining is not None and remaining <= 0:
                    return
                time.sleep(
                    min(
                        self.reconnect_delay,
                        remaining if remaining is not None else self.reconnect_delay,
                    )
                )

    def wait(
        self,
        job_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float = 1.0,
    ) -> JsonObject:
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            snapshot = self.snapshot(job_id)
            job = _object(snapshot.get("job"), "snapshot job")
            if job.get("status") in TERMINAL_STATUSES:
                return snapshot
            remaining = _remaining(deadline)
            if remaining is not None and remaining <= 0:
                raise CodasheError(
                    f"timed out waiting for job {job_id}",
                    code="wait_timeout",
                    retryable=True,
                )
            time.sleep(min(poll_interval, remaining or poll_interval))

    @contextmanager
    def _connection(self) -> Iterator[tuple[Any, str]]:
        version = self._protocol_version or str(self.health()["protocolVersion"])
        with self._websocket_factory(
            self.gateway_url,
            open_timeout=self.timeout,
            close_timeout=min(self.timeout, 5.0),
            max_size=1024 * 1024,
            proxy=None,
        ) as websocket:
            request_id = _identifier("handshake")
            websocket.send(
                json.dumps(
                    {
                        "type": "handshake",
                        "requestId": request_id,
                        "protocolVersion": version,
                        "client": {
                            "name": "cli-tools-codashe",
                            "version": __version__,
                        },
                    },
                    separators=(",", ":"),
                )
            )
            self._receive_reply(websocket, request_id)
            yield websocket, version

    def _receive_reply(self, websocket: Any, request_id: str) -> JsonObject:
        while True:
            message = self._receive(websocket, self.timeout)
            if message.get("type") == "error":
                self._raise_protocol_error(message)
            if message.get("requestId") != request_id:
                continue
            if message.get("type") not in {"ack", "snapshot"}:
                raise CodasheError("gateway returned an unexpected reply")
            return message

    @staticmethod
    def _receive(websocket: Any, timeout: float | None) -> JsonObject:
        raw = websocket.recv(timeout=timeout, decode=True)
        if not isinstance(raw, str):
            raise CodasheError("gateway returned a non-text WebSocket frame")
        return _object(json.loads(raw), "gateway WebSocket frame")

    @staticmethod
    def _raise_protocol_error(message: JsonObject) -> None:
        error = _object(message.get("error"), "protocol error")
        raise CodasheError(
            str(error.get("summary") or "codashe gateway rejected the operation"),
            code=str(error.get("code") or "protocol_error"),
            retryable=bool(error.get("retryable")),
            details=error.get("details"),
        )


def _object(value: Any, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise CodasheError(f"{label} must be a JSON object")
    return value


def _identifier(prefix: str) -> str:
    normalized = prefix.replace(".", "-").replace("_", "-")
    return f"{normalized}-{uuid.uuid4()}"


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _terminal_event(event: JsonObject) -> bool:
    if event.get("type") == "job.result":
        return True
    if event.get("type") != "job.status_changed":
        return False
    payload = event.get("payload")
    return isinstance(payload, dict) and payload.get("to") in TERMINAL_STATUSES
