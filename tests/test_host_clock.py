"""Tests for app/host_clock.py -- is this server's own clock trustworthy?"""

import asyncio
import struct
import time
from email.utils import formatdate
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import host_clock
from app.config import settings


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    host_clock._reset_for_tests()
    monkeypatch.setattr(settings, "host_clock_ntp_server", "pool.ntp.org")
    monkeypatch.setattr(settings, "host_clock_http_reference", "https://example.invalid")
    monkeypatch.setattr(settings, "host_clock_max_offset_seconds", 60)
    yield
    host_clock._reset_for_tests()


class _FakeNtpServer(asyncio.DatagramProtocol):
    """Answers every request with the local clock shifted by ``skew`` seconds."""

    def __init__(self, skew: float, stratum: int = 2) -> None:
        self.skew = skew
        self.stratum = stratum
        self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        now = time.time() + self.skew
        seconds = int(now) + host_clock._NTP_EPOCH_OFFSET
        fraction = int((now % 1) * 2**32)
        reply = bytearray(48)
        reply[0] = 0x1C  # LI=0, version 3, mode 4 (server)
        reply[1] = self.stratum
        reply[32:40] = struct.pack("!II", seconds, fraction)
        reply[40:48] = struct.pack("!II", seconds, fraction)
        assert self.transport is not None
        self.transport.sendto(bytes(reply), addr)


async def _serve_ntp(skew: float, stratum: int = 2):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _FakeNtpServer(skew, stratum), local_addr=("127.0.0.1", 0)
    )
    return transport, transport.get_extra_info("sockname")[1]


class TestQuerySntp:
    @pytest.mark.asyncio
    async def test_measures_host_minus_reference(self):
        transport, port = await _serve_ntp(skew=7200)
        try:
            offset = await host_clock.query_sntp("127.0.0.1", port=port, timeout=2.0)
        finally:
            transport.close()
        assert offset is not None
        # Reference is two hours ahead, so the host reads two hours behind it.
        assert abs(offset + 7200) < 1.0

    @pytest.mark.asyncio
    async def test_in_sync_reads_near_zero(self):
        transport, port = await _serve_ntp(skew=0)
        try:
            offset = await host_clock.query_sntp("127.0.0.1", port=port, timeout=2.0)
        finally:
            transport.close()
        assert offset is not None
        assert abs(offset) < 1.0

    @pytest.mark.asyncio
    async def test_kiss_of_death_is_ignored(self):
        transport, port = await _serve_ntp(skew=0, stratum=0)
        try:
            offset = await host_clock.query_sntp("127.0.0.1", port=port, timeout=2.0)
        finally:
            transport.close()
        assert offset is None

    @pytest.mark.asyncio
    async def test_silence_is_none(self):
        # Bind a socket that never answers, so the query has a real port to time out on.
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
        )
        port = transport.get_extra_info("sockname")[1]
        try:
            offset = await host_clock.query_sntp("127.0.0.1", port=port, timeout=0.3)
        finally:
            transport.close()
        assert offset is None


class TestQueryHttpDate:
    @pytest.mark.asyncio
    async def test_measures_host_minus_date_header(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Date": formatdate(time.time() + 3600, usegmt=True)}
            )

        offset = await host_clock.query_http_date(
            "https://example.invalid", transport=httpx.MockTransport(handler)
        )
        assert offset is not None
        assert abs(offset + 3600) < 2.0

    @pytest.mark.asyncio
    async def test_missing_date_header_is_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        offset = await host_clock.query_http_date(
            "https://example.invalid", transport=httpx.MockTransport(handler)
        )
        assert offset is None

    @pytest.mark.asyncio
    async def test_connection_failure_is_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")

        offset = await host_clock.query_http_date(
            "https://example.invalid", transport=httpx.MockTransport(handler)
        )
        assert offset is None


class TestCheckHostClock:
    @pytest.mark.asyncio
    async def test_small_offset_is_trusted_and_verified(self):
        with patch.object(host_clock, "query_sntp", AsyncMock(return_value=0.25)):
            status = await host_clock.check_host_clock()
        assert status.trusted is True
        assert status.verified is True
        assert status.source == "ntp"
        assert status.reference == "pool.ntp.org"
        assert status.offset_seconds == 0.25
        assert "verified via NTP" in status.message

    @pytest.mark.asyncio
    async def test_large_offset_is_not_trusted(self):
        with patch.object(host_clock, "query_sntp", AsyncMock(return_value=2 * 86400.0)):
            status = await host_clock.check_host_clock()
        assert status.trusted is False
        assert status.verified is True
        assert "ahead of the reference" in status.message
        assert "refused" in status.message

    @pytest.mark.asyncio
    async def test_http_is_the_fallback_when_ntp_fails(self):
        with (
            patch.object(host_clock, "query_sntp", AsyncMock(return_value=None)),
            patch.object(host_clock, "query_http_date", AsyncMock(return_value=-3.0)),
        ):
            status = await host_clock.check_host_clock()
        assert status.source == "http"
        assert status.reference == "https://example.invalid"
        assert status.trusted is True

    @pytest.mark.asyncio
    async def test_disabled_references_are_not_queried(self, monkeypatch):
        monkeypatch.setattr(settings, "host_clock_ntp_server", "")
        monkeypatch.setattr(settings, "host_clock_http_reference", "")
        with (
            patch.object(host_clock, "query_sntp", AsyncMock()) as sntp,
            patch.object(host_clock, "query_http_date", AsyncMock()) as http,
        ):
            status = await host_clock.check_host_clock()
        sntp.assert_not_awaited()
        http.assert_not_awaited()
        assert status.verified is False
        assert status.trusted is True  # unverified, but nothing suspicious either
        assert status.source == "none"

    @pytest.mark.asyncio
    async def test_result_is_cached_until_forced(self):
        sntp = AsyncMock(side_effect=[0.1, 5000.0])
        with patch.object(host_clock, "query_sntp", sntp):
            first = await host_clock.check_host_clock()
            second = await host_clock.check_host_clock()
            third = await host_clock.check_host_clock(force=True)
        assert first.trusted and second.trusted
        assert second is first
        assert third.trusted is False
        assert sntp.await_count == 2

    @pytest.mark.asyncio
    async def test_unverified_step_holds_pushes_then_lets_go(self):
        """A wall-clock jump with no reference to confirm it is quarantined, not forever."""
        base_wall = time.time()
        base_mono = time.monotonic()
        wall = {"value": base_wall}
        mono = {"value": base_mono}
        with (
            patch.object(host_clock, "_wall", lambda: wall["value"]),
            patch.object(host_clock, "_mono", lambda: mono["value"]),
            patch.object(host_clock, "query_sntp", AsyncMock(return_value=None)),
            patch.object(host_clock, "query_http_date", AsyncMock(return_value=None)),
        ):
            host_clock._reset_for_tests()  # anchor on the patched clocks
            calm = await host_clock.check_host_clock()
            assert calm.trusted is True

            # Two days pass on the wall, ten seconds on the monotonic clock: a jump.
            wall["value"] = base_wall + 2 * 86400
            mono["value"] = base_mono + 10
            jumped = await host_clock.check_host_clock()  # cache bypassed by the step
            assert jumped.trusted is False
            assert jumped.verified is False
            assert jumped.step_seconds == pytest.approx(2 * 86400 - 10, abs=1)
            assert "stepped" in jumped.message

            # Still inside the quarantine: held.
            mono["value"] = base_mono + 10 + host_clock.STEP_QUARANTINE_SECONDS / 2
            wall["value"] = base_wall + 2 * 86400 + host_clock.STEP_QUARANTINE_SECONDS / 2
            held = await host_clock.check_host_clock(force=True)
            assert held.trusted is False

            # Quarantine served with nothing to check against: released, re-anchored.
            mono["value"] = base_mono + 10 + host_clock.STEP_QUARANTINE_SECONDS + 1
            wall["value"] = base_wall + 2 * 86400 + host_clock.STEP_QUARANTINE_SECONDS + 1
            released = await host_clock.check_host_clock(force=True)
            assert released.trusted is True
            assert released.step_seconds == 0

    @pytest.mark.asyncio
    async def test_verified_check_clears_a_step(self):
        base_wall = time.time()
        base_mono = time.monotonic()
        wall = {"value": base_wall}
        mono = {"value": base_mono}
        with (
            patch.object(host_clock, "_wall", lambda: wall["value"]),
            patch.object(host_clock, "_mono", lambda: mono["value"]),
            patch.object(host_clock, "query_sntp", AsyncMock(return_value=0.0)),
        ):
            host_clock._reset_for_tests()
            await host_clock.check_host_clock()
            wall["value"] = base_wall + 300  # NTP just stepped us forward five minutes
            mono["value"] = base_mono + 1
            status = await host_clock.check_host_clock()
        # The reference says we are right now, so the step is history, not a hold.
        assert status.trusted is True
        assert status.step_seconds == pytest.approx(299, abs=1)
