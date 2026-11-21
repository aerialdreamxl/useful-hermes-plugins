# -*- coding: utf-8 -*-
"""gateway-ui approval response IPC.

审批请求展示仍走总线；审批决定不落共享文件。Windows 使用命名管道，
宿主读取客户端 PID；Unix 使用权限收紧的本地 socket，并尽力读取 peer PID。
worker 的握手密钥只经宿主已有的 stdin 管道传递，不写入 tray_meta.json。
"""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import socket
import struct
import tempfile
import threading
import time
import uuid
from pathlib import Path

_MAX_FRAME = 16 * 1024
_PIPE_PREFIX = r"\\.\pipe\hermes-gateway-ui-approval-"


def new_endpoint(bus_dir) -> str:
    if os.name == "nt":
        return _PIPE_PREFIX + uuid.uuid4().hex
    base = Path(bus_dir)
    name = f".approval-resp-{uuid.uuid4().hex}.sock"
    path = base / name
    if len(os.fsencode(str(path))) < 100:
        return str(path)
    return str(Path(tempfile.gettempdir()) / name)


def new_handshake() -> str:
    return secrets.token_urlsafe(48)


def _json_frame(payload: dict) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > _MAX_FRAME:
        raise ValueError("approval response is too large")
    return raw + b"\n"


def _decode_frame(raw: bytes) -> dict:
    if len(raw) > _MAX_FRAME:
        raise ValueError("approval response is too large")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("approval response must be an object")
    return data


def _close_handle(handle) -> None:
    if handle is not None and handle not in (0, -1):
        _KERNEL32.CloseHandle(handle)


if os.name == "nt":
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _ERROR_PIPE_CONNECTED = 535
    _ERROR_PIPE_BUSY = 231
    _PIPE_ACCESS_DUPLEX = 0x00000003
    _PIPE_TYPE_MESSAGE = 0x00000004
    _PIPE_READMODE_MESSAGE = 0x00000002
    _PIPE_WAIT = 0x00000000
    _PIPE_UNLIMITED_INSTANCES = 255
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3

    _KERNEL32.CreateNamedPipeW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    _KERNEL32.CreateNamedPipeW.restype = ctypes.c_void_p
    _KERNEL32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _KERNEL32.ConnectNamedPipe.restype = ctypes.c_int
    _KERNEL32.DisconnectNamedPipe.argtypes = [ctypes.c_void_p]
    _KERNEL32.DisconnectNamedPipe.restype = ctypes.c_int
    _KERNEL32.GetNamedPipeClientProcessId.argtypes = [ctypes.c_void_p,
                                                       ctypes.POINTER(ctypes.c_uint32)]
    _KERNEL32.GetNamedPipeClientProcessId.restype = ctypes.c_int
    _KERNEL32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                   ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    _KERNEL32.ReadFile.restype = ctypes.c_int
    _KERNEL32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    _KERNEL32.WriteFile.restype = ctypes.c_int
    _KERNEL32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
    _KERNEL32.FlushFileBuffers.restype = ctypes.c_int
    _KERNEL32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    _KERNEL32.CreateFileW.restype = ctypes.c_void_p
    _KERNEL32.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    _KERNEL32.WaitNamedPipeW.restype = ctypes.c_int
    _KERNEL32.SetNamedPipeHandleState.argtypes = [ctypes.c_void_p,
                                                   ctypes.POINTER(ctypes.c_uint32),
                                                   ctypes.c_void_p, ctypes.c_void_p]
    _KERNEL32.SetNamedPipeHandleState.restype = ctypes.c_int


def _win_read(handle) -> bytes:
    chunks = []
    total = 0
    while True:
        buf = ctypes.create_string_buffer(4096)
        got = ctypes.c_uint32(0)
        ok = _KERNEL32.ReadFile(handle, buf, len(buf), ctypes.byref(got), None)
        if got.value:
            chunks.append(buf.raw[:got.value])
            total += got.value
            if total > _MAX_FRAME:
                raise ValueError("approval response is too large")
        if ok:
            return b"".join(chunks)
        err = ctypes.get_last_error()
        if err != 234:  # ERROR_MORE_DATA
            raise OSError(err, "ReadFile failed")


def _win_write(handle, raw: bytes) -> None:
    sent = ctypes.c_uint32(0)
    buf = ctypes.create_string_buffer(raw)
    if not _KERNEL32.WriteFile(handle, buf, len(raw), ctypes.byref(sent), None):
        raise OSError(ctypes.get_last_error(), "WriteFile failed")
    _KERNEL32.FlushFileBuffers(handle)


def _win_client_pid(handle) -> int:
    pid = ctypes.c_uint32(0)
    if _KERNEL32.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
        return int(pid.value)
    return 0


class ApprovalResponseServer:
    """本地审批响应服务；callback(payload, peer_pid) 返回 dict。"""

    def __init__(self, endpoint, callback, logger=None):
        self.endpoint = str(endpoint)
        self.callback = callback
        self.logger = logger
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.thread = None
        self._sock = None

    def _log(self, msg):
        try:
            if self.logger is not None:
                self.logger.info("gateway-ui: approval IPC %s", msg)
        except Exception:
            pass

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name="approval-ipc", daemon=True)
        self.thread.start()
        self.ready_event.wait(1.0)

    def close(self):
        self.stop_event.set()
        if os.name == "nt":
            # Connect once to wake ConnectNamedPipe; the empty request is rejected.
            handle = None
            try:
                handle = ApprovalResponseClient._connect_windows(self.endpoint)
            except Exception:
                pass
            finally:
                _close_handle(handle)
        else:
            try:
                af_unix = getattr(socket, "AF_UNIX", None)
                if af_unix is None:
                    raise OSError("local sockets unavailable")
                with socket.socket(af_unix, socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    s.connect(self.endpoint)
            except Exception:
                pass
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        self.thread = None
        if os.name != "nt":
            Path(self.endpoint).unlink(missing_ok=True)

    def _dispatch(self, raw, peer_pid):
        try:
            payload = _decode_frame(raw)
            result = self.callback(payload, int(peer_pid or 0))
            return result if isinstance(result, dict) else {"ok": bool(result)}
        except Exception as exc:
            self._log(f"request rejected: {type(exc).__name__}")
            return {"ok": False}

    def _run(self):
        if os.name == "nt":
            self._run_windows()
        else:
            self._run_unix()

    def _run_windows(self):
        while not self.stop_event.is_set():
            handle = _KERNEL32.CreateNamedPipeW(
                self.endpoint, _PIPE_ACCESS_DUPLEX,
                _PIPE_TYPE_MESSAGE | _PIPE_READMODE_MESSAGE | _PIPE_WAIT,
                _PIPE_UNLIMITED_INSTANCES, 4096, 4096, 1000, None)
            if handle in (None, _INVALID_HANDLE_VALUE):
                self._log("CreateNamedPipeW failed")
                self.ready_event.set()
                return
            self.ready_event.set()
            try:
                connected = _KERNEL32.ConnectNamedPipe(handle, None)
                if not connected and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                    continue
                if self.stop_event.is_set():
                    continue
                peer_pid = _win_client_pid(handle)
                try:
                    result = self._dispatch(_win_read(handle), peer_pid)
                except Exception:
                    result = {"ok": False}
                try:
                    _win_write(handle, _json_frame(result))
                except Exception:
                    pass
            finally:
                try:
                    _KERNEL32.DisconnectNamedPipe(handle)
                except Exception:
                    pass
                _close_handle(handle)

    def _run_unix(self):
        path = Path(self.endpoint)
        path.unlink(missing_ok=True)
        af_unix = getattr(socket, "AF_UNIX", None)
        if af_unix is None:
            self._log("local sockets unavailable")
            return
        sock = socket.socket(af_unix, socket.SOCK_STREAM)
        self._sock = sock
        try:
            sock.bind(self.endpoint)
            os.chmod(self.endpoint, 0o600)
            sock.listen(4)
            self.ready_event.set()
            sock.settimeout(0.5)
            while not self.stop_event.is_set():
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                with conn:
                    conn.settimeout(1.0)
                    raw = _recv_unix_frame(conn)
                    peer_pid = 0
                    try:
                        peer_cred = getattr(socket, "SO_PEERCRED", None)
                        if peer_cred is None:
                            raise OSError("peer credentials unavailable")
                        creds = conn.getsockopt(socket.SOL_SOCKET, peer_cred, 12)
                        peer_pid = struct.unpack("3i", creds)[1]
                    except Exception:
                        pass
                    result = self._dispatch(raw, peer_pid)
                    try:
                        conn.sendall(_json_frame(result))
                    except Exception:
                        pass
        finally:
            self.ready_event.set()
            sock.close()
            self._sock = None


def _recv_unix_frame(conn) -> bytes:
    chunks = []
    total = 0
    while True:
        part = conn.recv(4096)
        if not part:
            break
        chunks.append(part)
        total += len(part)
        if total > _MAX_FRAME:
            raise ValueError("approval response is too large")
        if b"\n" in part:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


class ApprovalResponseClient:
    """发送一次审批注册/响应，不持有共享文件句柄。"""

    @staticmethod
    def _connect_windows(endpoint):
        # 服务线程处理完一次连接后要先关旧实例再建新管道，这个窗口期
        # CreateFileW 会报 FILE_NOT_FOUND/NO_DATA；调用方 send() 带超时重试。
        handle = _KERNEL32.CreateFileW(endpoint, _GENERIC_READ | _GENERIC_WRITE,
                                       0, None, _OPEN_EXISTING, 0, None)
        if handle not in (None, _INVALID_HANDLE_VALUE):
            return handle
        err = ctypes.get_last_error()
        if err == _ERROR_PIPE_BUSY:
            _KERNEL32.WaitNamedPipeW(endpoint, 1000)
            handle = _KERNEL32.CreateFileW(endpoint, _GENERIC_READ | _GENERIC_WRITE,
                                           0, None, _OPEN_EXISTING, 0, None)
        if handle in (None, _INVALID_HANDLE_VALUE):
            raise OSError(err, "CreateFileW failed")
        mode = ctypes.c_uint32(_PIPE_READMODE_MESSAGE)
        _KERNEL32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None)
        return handle

    @classmethod
    def send(cls, endpoint, payload, timeout=1.5):
        raw = _json_frame(payload)
        deadline = time.monotonic() + max(float(timeout), 0.2)
        last_exc = None
        while True:
            try:
                return cls._send_once(endpoint, raw, timeout)
            except OSError as exc:
                # 实例更替窗口（FILE_NOT_FOUND/管道忙/连接被拒）→ 退避重试到超时
                last_exc = exc
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    @classmethod
    def _send_once(cls, endpoint, raw, timeout):
        if os.name == "nt":
            handle = cls._connect_windows(str(endpoint))
            try:
                _win_write(handle, raw)
                return _decode_frame(_win_read(handle))
            finally:
                _close_handle(handle)
        af_unix = getattr(socket, "AF_UNIX", None)
        if af_unix is None:
            raise OSError("local sockets unavailable")
        with socket.socket(af_unix, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(endpoint))
            sock.sendall(raw)
            return _decode_frame(sock.recv(_MAX_FRAME + 1))


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = kernel32.OpenProcess(0x1000, False, int(pid))
            if h:
                kernel32.CloseHandle(h)
                return True
            return False
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False
