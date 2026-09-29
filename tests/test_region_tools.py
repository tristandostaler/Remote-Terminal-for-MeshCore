import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.region_resolver import compute_transport_code
from app.repository import AppSettingsRepository
from app.services import region_tools

GROUP_TEXT = 0x05


def _scoped_packet(region: str, payload: bytes) -> bytes:
    """TRANSPORT_FLOOD (route 0) GROUP_TEXT packet scoped to ``region``."""
    code = compute_transport_code(region, GROUP_TEXT, payload)
    assert code is not None
    header = (GROUP_TEXT << 2) | 0x00
    return bytes([header]) + code.to_bytes(2, "little") + b"\x00\x00" + b"\x00" + payload


def _packets(region: str, count: int) -> list[bytes]:
    return [_scoped_packet(region, bytes([i]) * 8 + b"payload") for i in range(count)]


def test_guess_finds_region_that_explains_multiple_packets():
    rows = _packets("yul", 4)
    packets, scoped = region_tools.scoped_packets_from_rows(rows, [], limit=50)
    assert scoped == 4 and len(packets) == 4

    results, timed_out, _ = region_tools.guess_regions(packets, ["yul", "yyz", "onqc"], min_hits=2)

    assert not timed_out
    assert [(r.region, r.hits) for r in results] == [("yul", 4)]


def test_guess_skips_packets_explained_by_known_regions():
    rows = _packets("yul", 3) + _packets("yyz", 3)
    packets, scoped = region_tools.scoped_packets_from_rows(rows, ["yul"], limit=50)
    # yul packets are already explained; yyz packets remain to test.
    assert scoped == 6
    assert len(packets) == 3


def test_guess_min_hits_filters_chance_matches():
    packets, _ = region_tools.scoped_packets_from_rows(_packets("yul", 1), [], limit=10)
    results, _, _ = region_tools.guess_regions(packets, ["yul"], min_hits=2)
    assert results == []


def test_brute_force_candidates_cover_letter_range():
    names = list(region_tools.brute_force_candidates(2, 3))
    assert len(names) == 26**2 + 26**3
    assert {"qc", "yul"} <= set(names)
    assert list(region_tools.brute_force_candidates(1, 1)) == list("abcdefghijklmnopqrstuvwxyz")
    assert region_tools.brute_force_total(4, 8) == sum(26**n for n in range(4, 9))
    # Lazy: an 8-letter sweep must not be materialized up front.
    assert next(iter(region_tools.brute_force_candidates(8, 8))) == "aaaaaaaa"


def test_expand_candidates_tries_typed_and_lowercase_and_rejects_junk():
    names = list(
        region_tools.expand_candidates(
            ["#QC", "bad name", "*", ""], min_letters=None, max_letters=None
        )
    )
    assert names == ["QC", "qc"]


def test_extract_names_from_json_html_and_text():
    assert region_tools.extract_region_names(
        '{"regions": [{"name": "yul"}, {"code": "onqc"}, "qc"]}', "application/json"
    ) == ["yul", "onqc", "qc"]
    html = "<ul><li>yul</li><li>onqc, qc</li></ul><p>Use #yyz for Toronto.</p><script>x</script>"
    assert set(region_tools.extract_region_names(html, "text/html")) == {
        "yul",
        "onqc",
        "qc",
        "yyz",
    }
    assert region_tools.extract_region_names("yul\nqc, on\n", "text/plain") == ["yul", "qc", "on"]


def test_extract_names_lowercases_and_dedupes():
    assert region_tools.extract_region_names("YUL\nyul\nOnQc, QC\n", "text/plain") == [
        "yul",
        "onqc",
        "qc",
    ]
    assert region_tools.extract_region_names('["YUL", "Yyz"]', "application/json") == [
        "yul",
        "yyz",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "http://127.0.0.1:8000/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/",
        "not a url",
    ],
)
def test_import_rejects_non_public_or_non_http_urls(url):
    with pytest.raises(region_tools.RegionImportError):
        region_tools.validate_import_url(url)


async def _seed_yul(test_db):
    for raw in _packets("yul", 3):
        await test_db.conn.execute(
            "INSERT INTO raw_packets (timestamp, data, payload_hash) VALUES (?, ?, ?)",
            (1_700_000_000, raw, raw[-32:].ljust(32, b"\0")),
        )
    await test_db.conn.commit()
    await AppSettingsRepository.update(known_regions=[])


async def _run_job(client, body):
    started = await client.post("/api/settings/regions/guess", json=body)
    assert started.status_code == 200
    for _ in range(200):
        job = (await client.get("/api/settings/regions/guess")).json()
        if job["status"] != "running":
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


@pytest.mark.asyncio
async def test_guess_job_finds_region_from_explicit_candidates(test_db):
    await _seed_yul(test_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        job = await _run_job(client, {"candidates": ["yul"], "brute_force": False})

    assert job["status"] == "completed"
    assert job["tested_packets"] == 3
    assert [(r["region"], r["hits"]) for r in job["results"]] == [("yul", 3)]


@pytest.mark.asyncio
async def test_guess_job_brute_forces_letter_range(test_db):
    await _seed_yul(test_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        found = await _run_job(client, {"min_letters": 3, "max_letters": 3})
        missed = await _run_job(client, {"min_letters": 2, "max_letters": 2})
        bad = await client.post(
            "/api/settings/regions/guess", json={"min_letters": 3, "max_letters": 2}
        )

    assert [r["region"] for r in found["results"]] == ["yul"]
    assert found["candidates_total"] == 26**3
    assert missed["results"] == []
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_guess_job_runs_in_background_and_can_be_cancelled(test_db):
    await _seed_yul(test_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        # 10-letter names are unfinishable; no time limit, so only cancel ends it.
        started = await client.post(
            "/api/settings/regions/guess",
            json={"min_letters": 10, "max_letters": 10, "max_seconds": 0},
        )
        job_id = started.json()["job_id"]
        assert started.json()["status"] == "running"
        busy = await client.post("/api/settings/regions/guess", json={})
        assert busy.status_code == 409
        await client.post(f"/api/settings/regions/guess/{job_id}/cancel")
        for _ in range(200):
            job = (await client.get("/api/settings/regions/guess")).json()
            if job["status"] != "running":
                break
            await asyncio.sleep(0.05)
        missing = await client.post("/api/settings/regions/guess/nope/cancel")

    assert job["status"] == "cancelled" and job["max_seconds"] is None
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_import_endpoint_rejects_private_url(test_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            "/api/settings/regions/import", json={"url": "http://127.0.0.1/regions"}
        )
    assert response.status_code == 400


def test_guess_stops_at_time_limit_and_reports_names_tried():
    packets, _ = region_tools.scoped_packets_from_rows(_packets("yul", 3), [], limit=10)
    seen = []
    results, timed_out, tried = region_tools.guess_regions(
        packets,
        region_tools.brute_force_candidates(8, 8),
        max_seconds=0.2,
        on_progress=lambda n, found: seen.append(n),
    )
    assert timed_out and results == []
    assert 0 < tried < 26**8
    assert seen  # progress was reported along the way
