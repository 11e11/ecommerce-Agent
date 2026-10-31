"""Single-process Uvicorn lifecycle with bind-before-start support for pilot mode."""
from __future__ import annotations

import socket
from contextlib import contextmanager

import uvicorn


class ASGIServer:
    def __init__(self, app, host: str, port: int):
        self.app = app
        self.manage_signals = True
        self.socket = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.bind((host, port))
            self.socket.listen(128)
        except BaseException:
            self.socket.close()
            raise
        self.server_address = self.socket.getsockname()
        self.server_port = self.server_address[1]
        self.config = uvicorn.Config(app, host=host, port=self.server_port, workers=1,
            access_log=False, log_config=None, proxy_headers=False, timeout_graceful_shutdown=5)
        owner = self

        class ManagedServer(uvicorn.Server):
            @contextmanager
            def capture_signals(self):
                if owner.manage_signals:
                    with super().capture_signals():
                        yield
                else:
                    yield

        self.server = ManagedServer(self.config)

    def serve_forever(self):
        self.server.run(sockets=[self.socket])

    def shutdown(self):
        self.server.should_exit = True

    def server_close(self):
        self.server.should_exit = True
        self.socket.close()
