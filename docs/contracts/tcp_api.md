# Contract: TCP API (`src/tcp.py`) — FROZEN v1

Language: Python 3.10, standard library only. Single-threaded: every blocking call
drives the internal event loop (raw socket + timers) itself. No threads.

```python
from tcp import (TCPConnection, TCPListener,
                 TCPError, ConnectionRefused, ConnectionReset, TCPTimeout, RecvTimeout)

class TCPError(Exception): ...            # base class
class ConnectionRefused(TCPError): ...    # RST received during handshake
class ConnectionReset(TCPError): ...      # RST received after handshake
class TCPTimeout(TCPError): ...           # handshake gave up (>=6 s) or nothing ACKed for 10 s
class RecvTimeout(TCPError): ...          # recv(timeout=...) expired; connection still usable

class TCPConnection:
    local_ip: str; local_port: int; remote_ip: str; remote_port: int

    @classmethod
    def connect(cls, dst_ip: str, dst_port: int, log=None) -> "TCPConnection"
        # Active open. Source IP from kernel routing table, random port 61000-65535.
        # `log` is a PacketLog instance or None.

    def send(self, data: bytes) -> None
        # Queues data; returns once queued (may block for flow control while the
        # internal buffer is large). Raises TCPError on failure.

    def recv(self, max_bytes: int = 65536, timeout: float | None = None) -> bytes
        # Returns 1..max_bytes in-order bytes, or b"" once the peer's FIN has been
        # reached (EOF). timeout=None blocks; on expiry raises RecvTimeout.

    def close(self) -> None
        # Graceful teardown (T9): waits until all queued data is ACKed, sends FIN,
        # retransmits until ACKed, waits for peer FIN; FIN phase gives up after 2 s.
        # Idempotent. Never raises (swallows errors).

    def abort(self) -> None   # drop connection silently (no packets)

class TCPListener:
    def __init__(self, port: int, log=None)    # binds raw socket; any local address
    def accept(self) -> TCPConnection          # blocks until one handshake completes
    def close(self) -> None
```

Packet log: `from packet import PacketLog`; `PacketLog(path)` opens the file once
(truncate), `.write_line(str)` flushes each line. HTTP programs create it from
`--log FILE` and pass it to `connect()` / `TCPListener()`.

Exit-code mapping for the HTTP client: any `TCPError` or `OSError` -> exit 2.
