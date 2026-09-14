import socket

import pytest

from forge.ui.server import probe_port


def test_probe_port_advances_past_a_busy_port_unless_strict():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        assert probe_port(port, strict=False) != port
        with pytest.raises(OSError, match="in use"):
            probe_port(port, strict=True)
