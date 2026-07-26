from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import time
from typing import Any, Callable
from urllib import error, parse, request


class ComfyUIError(RuntimeError):
    pass


class ComfyUIClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8188", timeout_seconds: int = 180) -> None:
        parsed = parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("ComfyUI URL must use loopback HTTP(S)")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.client_id = secrets.token_hex(16)

    def _value(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(self.base_url + path, data=body, method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=timeout_seconds or self.timeout_seconds) as response:
                raw = response.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ComfyUIError(f"ComfyUI HTTP {exc.code}: {detail}") from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise ComfyUIError(f"Cannot reach ComfyUI at {self.base_url}: {exc}") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ComfyUIError("ComfyUI returned invalid JSON") from exc
        return value

    def _json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        value = self._value(method, path, payload, timeout_seconds)
        if not isinstance(value, dict):
            raise ComfyUIError("ComfyUI JSON response must be an object")
        return value

    def system_stats(self) -> dict[str, Any]:
        return self._json("GET", "/system_stats")

    def model_folders(self) -> list[str]:
        folders = self._value("GET", "/models")
        if not isinstance(folders, list):
            raise ComfyUIError("ComfyUI model folder response is invalid")
        return sorted({str(folder) for folder in folders if isinstance(folder, str) and folder})

    def models_in_folder(self, folder: str) -> list[str]:
        if not folder or "/" in folder or "\\" in folder or folder in {".", ".."}:
            raise ComfyUIError("Unsafe ComfyUI model folder")
        models = self._value("GET", f"/models/{parse.quote(folder, safe='')}")
        if not isinstance(models, list):
            raise ComfyUIError(f"ComfyUI model response is invalid for {folder}")
        return sorted({str(model) for model in models if isinstance(model, str) and model})

    def model_inventory(self) -> dict[str, list[str]]:
        return {
            folder: self.models_in_folder(folder)
            for folder in self.model_folders()
        }

    def queue_prompt(self, workflow: dict[str, Any]) -> str:
        value = self._json("POST", "/prompt", {"prompt": workflow, "client_id": self.client_id})
        prompt_id = value.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ComfyUIError(f"ComfyUI rejected prompt: {value}")
        return prompt_id

    def history(self, prompt_id: str) -> dict[str, Any] | None:
        value = self._json("GET", f"/history/{parse.quote(prompt_id, safe='')}")
        item = value.get(prompt_id)
        return item if isinstance(item, dict) else None

    def cancel(self, prompt_id: str, *, interrupt: bool = True) -> None:
        self._json("POST", "/queue", {"delete": [prompt_id]})
        if interrupt:
            self._json("POST", "/interrupt", {})

    def wait(
        self,
        prompt_id: str,
        timeout_seconds: int,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        websocket_error: str | None = None
        try:
            with _ComfyWebSocket(self.base_url, self.client_id, min(timeout_seconds, 30)) as websocket:
                while time.monotonic() < deadline:
                    event = websocket.read_event(max(0.1, min(5.0, deadline - time.monotonic())))
                    if event is None:
                        continue
                    if on_progress:
                        on_progress(event)
                    data = event.get("data", {})
                    if (
                        event.get("type") == "executing"
                        and isinstance(data, dict)
                        and data.get("prompt_id") == prompt_id
                        and data.get("node") is None
                    ):
                        history = self.history(prompt_id)
                        if history is not None:
                            return history
        except (ComfyUIError, OSError, TimeoutError) as exc:
            websocket_error = str(exc)

        while time.monotonic() < deadline:
            history = self.history(prompt_id)
            if history is not None and history.get("outputs"):
                return history
            time.sleep(min(1.0, max(0.1, deadline - time.monotonic())))
        detail = f"; WebSocket: {websocket_error}" if websocket_error else ""
        raise ComfyUIError(f"ComfyUI prompt timed out after {timeout_seconds}s{detail}")

    def download_output(self, filename: str, subfolder: str = "", kind: str = "output") -> bytes:
        if Path(filename).name != filename or ".." in Path(subfolder).parts:
            raise ComfyUIError("Unsafe ComfyUI output path")
        query = parse.urlencode({"filename": filename, "subfolder": subfolder, "type": kind})
        req = request.Request(f"{self.base_url}/view?{query}", method="GET")
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                return response.read()
        except (error.URLError, error.HTTPError, TimeoutError, OSError) as exc:
            raise ComfyUIError(f"Cannot download ComfyUI output {filename}: {exc}") from exc


def start_portable_comfyui(root: Path, runtime_path: Path, log_path: Path) -> dict[str, Any]:
    root = root.resolve()
    python = root / "python_embeded" / "python.exe"
    main = root / "ComfyUI" / "main.py"
    if not python.is_file() or not main.is_file():
        raise ComfyUIError("Portable ComfyUI root lacks embedded Python or ComfyUI/main.py")
    if runtime_path.exists():
        existing = json.loads(runtime_path.read_text(encoding="utf-8"))
        pid = existing.get("pid")
        if isinstance(pid, int) and process_matches(pid, python):
            return existing
        runtime_path.unlink(missing_ok=True)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    process = subprocess.Popen(
        [
            str(python), "-s", str(main),
            "--windows-standalone-build", "--listen", "127.0.0.1", "--port", "8188",
        ],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        close_fds=True,
    )
    log.close()
    runtime = {
        "schema_version": "1.0",
        "created_at": time.time(),
        "pid": process.pid,
        "executable": str(python),
        "main": str(main),
        "root": str(root),
        "url": "http://127.0.0.1:8188",
        "log": str(log_path),
    }
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")
    return runtime


def stop_portable_comfyui(runtime_path: Path) -> dict[str, Any]:
    if not runtime_path.exists():
        raise ComfyUIError("No Head Chef-managed ComfyUI runtime record exists")
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    pid = runtime.get("pid")
    executable = runtime.get("executable")
    if not isinstance(pid, int) or not isinstance(executable, str):
        raise ComfyUIError("Invalid ComfyUI runtime record")
    if process_matches(pid, Path(executable)):
        try:
            os.kill(pid, 15)
        except OSError as exc:
            raise ComfyUIError(f"Cannot stop managed ComfyUI PID {pid}: {exc}") from exc
    runtime_path.unlink(missing_ok=True)
    return runtime


def process_matches(pid: int, executable: Path) -> bool:
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return False
            try:
                size = wintypes.DWORD(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    process, 0, buffer, ctypes.byref(size)
                ):
                    return False
                actual = Path(buffer.value).resolve()
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        else:
            actual = Path(f"/proc/{pid}/exe").resolve()
        return actual == executable.resolve()
    except (OSError, ValueError):
        return False


class _ComfyWebSocket:
    def __init__(self, base_url: str, client_id: str, timeout_seconds: float) -> None:
        parsed = parse.urlparse(base_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.secure = parsed.scheme == "https"
        self.path = f"/ws?clientId={parse.quote(client_id)}"
        self.timeout_seconds = timeout_seconds
        self.sock: socket.socket | None = None

    def __enter__(self) -> "_ComfyWebSocket":
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout_seconds)
        if self.secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request_text = (
            f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request_text.encode("ascii"))
        response = _recv_until(sock, b"\r\n\r\n", 16384)
        if not response.startswith(b"HTTP/1.1 101"):
            sock.close()
            raise ComfyUIError("ComfyUI WebSocket upgrade failed")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if f"sec-websocket-accept: {expected}".encode("ascii") not in response.lower():
            sock.close()
            raise ComfyUIError("ComfyUI WebSocket handshake was not authenticated")
        self.sock = sock
        return self

    def __exit__(self, *_: object) -> None:
        if self.sock:
            self.sock.close()

    def read_event(self, timeout_seconds: float) -> dict[str, Any] | None:
        if self.sock is None:
            raise ComfyUIError("WebSocket is not connected")
        self.sock.settimeout(timeout_seconds)
        try:
            first = _recv_exact(self.sock, 2)
        except socket.timeout:
            return None
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", _recv_exact(self.sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exact(self.sock, 8))[0]
        if length > 2_000_000:
            raise ComfyUIError("ComfyUI WebSocket frame is too large")
        mask = _recv_exact(self.sock, 4) if masked else b""
        payload = _recv_exact(self.sock, length)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if opcode == 8:
            raise ComfyUIError("ComfyUI WebSocket closed")
        if opcode != 1:
            return None
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise ComfyUIError("ComfyUI WebSocket disconnected")
        chunks.extend(chunk)
    return bytes(chunks)


def _recv_until(sock: socket.socket, marker: bytes, limit: int) -> bytes:
    value = bytearray()
    while marker not in value:
        chunk = sock.recv(1024)
        if not chunk:
            raise ComfyUIError("ComfyUI WebSocket disconnected during handshake")
        value.extend(chunk)
        if len(value) > limit:
            raise ComfyUIError("ComfyUI WebSocket handshake is too large")
    return bytes(value)
