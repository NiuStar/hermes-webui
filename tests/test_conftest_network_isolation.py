"""Adversarial test for the network-isolation fixture in conftest.py.

The autouse module-level monkey-patch in tests/conftest.py wraps
socket.create_connection so that any non-loopback / non-RFC1918 / non-link-local
destination raises OSError. This file proves:

  1. The block actually fires for outbound to a real public IP.
  2. Loopback / RFC1918 / link-local / reserved-TLD destinations pass through.
  3. The `allow_outbound_network` fixture re-enables real network for tests
     that legitimately need it.

Without this enforcement, a test that accidentally calls real outbound
(forgotten mock, leaked credential triggering an SDK initialisation, new
code path bypassing an existing mock) can leak production credentials,
slow the test suite into 10-minute waits on TLS handshakes, and produce
flaky failures depending on whether the destination is reachable.
"""
from __future__ import annotations

import socket
import pytest


def test_outbound_to_public_ipv4_is_blocked():
    """Attempting to connect to a public IP must raise OSError."""
    with pytest.raises(OSError, match="hermes test network isolation"):
        # 8.8.8.8 (Google DNS) is a stable real public IPv4.
        # If we accidentally connect, the test goes to 53/tcp which is
        # genuinely listening — so the block is what stops us, not lack of
        # destination.
        socket.create_connection(("8.8.8.8", 53), timeout=1)


def test_outbound_to_anthropic_ipv6_is_blocked():
    """The exact destination we observed leaking from earlier pytest runs."""
    with pytest.raises(OSError, match="hermes test network isolation"):
        socket.create_connection(("2607:6bc0::10", 443), timeout=1)


def test_outbound_to_amazon_is_blocked():
    """AWS endpoints (botocore / bedrock) must not reach the real service."""
    with pytest.raises(OSError, match="hermes test network isolation"):
        socket.create_connection(("3.173.21.63", 443), timeout=1)


def test_loopback_v4_is_allowed():
    """127.0.0.1 must continue to work — test_server fixture depends on it."""
    # Listen on a temporary port + connect via the wrapped create_connection.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    try:
        client = socket.create_connection(("127.0.0.1", port), timeout=1)
        client.close()
    finally:
        listener.close()


def test_rfc1918_private_ipv4_is_allowed():
    """RFC1918 (10/8, 172.16/12, 192.168/16) must pass — devs run LM Studio
    on their LAN. The block only refuses non-RFC1918 + non-loopback."""
    import tests.conftest as _conftest
    # Direct unit test on the predicate so we don't have to start a real listener
    # in a private-IP subnet just to prove this.
    assert _conftest._hermes_addr_is_local("10.0.0.5") is True
    assert _conftest._hermes_addr_is_local("172.16.5.1") is True
    assert _conftest._hermes_addr_is_local("172.31.255.254") is True
    assert _conftest._hermes_addr_is_local("192.168.1.22") is True


def test_link_local_is_allowed():
    """169.254.0.0/16 (link-local / IMDS) — AWS_EC2_METADATA_DISABLED already
    short-circuits the actual probe but the socket layer allows it."""
    import tests.conftest as _conftest
    assert _conftest._hermes_addr_is_local("169.254.169.254") is True


def test_reserved_tlds_are_allowed():
    """RFC 2606/6761 reserved TLDs — used as documentation hostnames in tests
    (e.g. example.com, test-host.invalid)."""
    import tests.conftest as _conftest
    assert _conftest._hermes_addr_is_local("example.com") is True
    assert _conftest._hermes_addr_is_local("my-mac.tailnet.example") is True
    assert _conftest._hermes_addr_is_local("anything.invalid") is True
    assert _conftest._hermes_addr_is_local("test-host.test") is True
    assert _conftest._hermes_addr_is_local("printer.local") is True
    assert _conftest._hermes_addr_is_local("localhost") is True


def test_public_ipv4_is_blocked():
    """Public IPs must NOT be treated as local."""
    import tests.conftest as _conftest
    assert _conftest._hermes_addr_is_local("8.8.8.8") is False
    assert _conftest._hermes_addr_is_local("1.1.1.1") is False
    assert _conftest._hermes_addr_is_local("203.0.113.0") is True  # TEST-NET-3
    assert _conftest._hermes_addr_is_local("204.0.113.0") is False  # outside


def test_allow_outbound_network_fixture_unswaps_the_wrappers(
    allow_outbound_network, monkeypatch,
):
    """Reach DNS for a public destination without making a real connection."""
    class ReachedResolver(Exception):
        pass

    def resolver(host, port, *args, **kwargs):
        assert (host, port) == ("8.8.8.8", 53)
        raise ReachedResolver

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    with pytest.raises(ReachedResolver):
        socket.create_connection(("8.8.8.8", 53), timeout=1)


def test_block_is_active_outside_the_fixture():
    """Collection may add server.py's guard; enforce behavior, not its name."""
    with pytest.raises(OSError, match="hermes test network isolation"):
        socket.create_connection(("8.8.8.8", 53), timeout=1)
    with socket.socket() as client:
        with pytest.raises(OSError, match="hermes test network isolation"):
            client.connect(("8.8.8.8", 53))


@pytest.mark.parametrize("fail_in_body", [False, True])
def test_opt_in_restores_exact_wrapper_chain(fail_in_body):
    """Preserve even an additional collection-installed guard on every exit."""
    import tests.conftest as conftest

    before = (socket.create_connection, socket.socket.connect)
    try:
        with pytest.MonkeyPatch.context() as patch:
            fixture = conftest.allow_outbound_network.__wrapped__(patch)
            next(fixture)
            try:
                assert socket.create_connection is conftest._REAL_CREATE_CONNECTION
                assert socket.socket.connect is conftest._REAL_SOCKET_CONNECT
                if fail_in_body:
                    raise RuntimeError("simulated test failure")
            finally:
                with pytest.raises(StopIteration):
                    next(fixture)
    except RuntimeError as exc:
        assert str(exc) == "simulated test failure"
    assert (socket.create_connection, socket.socket.connect) == before
    test_block_is_active_outside_the_fixture()
    test_loopback_v4_is_allowed()


@pytest.mark.parametrize("host", ["10.0.0.5", "172.16.5.1", "192.168.1.22", "169.254.169.254"])
def test_private_destinations_reach_transport(monkeypatch, host):
    """Both installed wrapper chains allow private IPs, without LAN traffic."""
    import tests.conftest as conftest

    calls = []
    marker = object()

    def create(address, *args, **kwargs):
        calls.append(address)
        return marker

    def connect(client, address):
        calls.append(address)

    monkeypatch.setattr(conftest, "_REAL_CREATE_CONNECTION", create)
    monkeypatch.setattr(conftest, "_REAL_SOCKET_CONNECT", connect)
    assert socket.create_connection((host, 443), timeout=1) is marker
    with socket.socket() as client:
        client.connect((host, 443))
    assert calls == [(host, 443), (host, 443)]
