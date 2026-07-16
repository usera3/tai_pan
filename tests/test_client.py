from urllib.parse import parse_qs

import httpx
import pytest

from app.client import (
    TmpLinkBusinessError,
    TmpLinkClient,
    TmpLinkConnectionError,
    TmpLinkTimeoutError,
)


def form_data(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode("utf-8"))
    return {key: values[0] for key, values in parsed.items()}


@pytest.mark.asyncio
async def test_direct_actions_map_to_documented_forms():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": 1, "data": {"ok": True}})

    client = TmpLinkClient("test-key", transport=httpx.MockTransport(handler))

    await client.quota()
    await client.list_files(page=2)
    await client.list_links(page=3)
    await client.create_link("FILE-UKEY", valid_time=60, download_limit=3)
    await client.delete_link("DIRECT-DKEY", delete_file=True)

    assert form_data(requests[0]) == {"action": "quota", "key": "test-key"}
    assert form_data(requests[1]) == {
        "action": "list_of_workspace",
        "key": "test-key",
        "page": "2",
    }
    assert form_data(requests[2]) == {
        "action": "list_of_direct",
        "key": "test-key",
        "page": "3",
    }
    assert form_data(requests[3]) == {
        "action": "link_add",
        "key": "test-key",
        "ukey": "FILE-UKEY",
        "valid_time": "60",
        "download_limit": "3",
    }
    assert form_data(requests[4]) == {
        "action": "link_del",
        "key": "test-key",
        "dkey": "DIRECT-DKEY",
        "delete": "1",
    }


@pytest.mark.asyncio
async def test_optional_link_fields_are_omitted():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"status": "1", "data": {}})

    client = TmpLinkClient("test-key", transport=httpx.MockTransport(handler))
    await client.create_link("FILE-UKEY", valid_time=None, download_limit=None)
    await client.delete_link("DIRECT-DKEY", delete_file=False)

    assert form_data(captured[0]) == {
        "action": "link_add",
        "key": "test-key",
        "ukey": "FILE-UKEY",
    }
    assert form_data(captured[1]) == {
        "action": "link_del",
        "key": "test-key",
        "dkey": "DIRECT-DKEY",
        "delete": "0",
    }


@pytest.mark.asyncio
async def test_upload_uses_documented_multipart_fields():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"status": 1, "data": "UPLOADED-UKEY"})

    client = TmpLinkClient("test-key", transport=httpx.MockTransport(handler))

    result = await client.upload("report.txt", b"hello", model=99)

    request = captured[0]
    body = request.content
    assert str(request.url) == "https://tmp-cli.vx-cdn.com/app/upload_cli"
    assert b'name="key"' in body and b"test-key" in body
    assert b'name="model"' in body and b"99" in body
    assert b'filename="report.txt"' in body and b"hello" in body
    assert result.data == "UPLOADED-UKEY"


@pytest.mark.asyncio
async def test_upload_rejects_unknown_storage_model():
    client = TmpLinkClient("test-key", transport=httpx.MockTransport(lambda request: None))

    with pytest.raises(ValueError, match="storage model"):
        await client.upload("report.txt", b"hello", model=5)


@pytest.mark.asyncio
async def test_business_error_is_descriptive_and_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 6, "message": "API Key invalid"})

    client = TmpLinkClient("test-key", transport=httpx.MockTransport(handler))

    with pytest.raises(TmpLinkBusinessError, match="API Key invalid") as error:
        await client.quota()

    assert "test-key" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remote_error", "expected_error"),
    [
        (httpx.ReadTimeout("late"), TmpLinkTimeoutError),
        (httpx.ConnectError("offline"), TmpLinkConnectionError),
    ],
)
async def test_network_errors_are_translated_and_redacted(remote_error, expected_error):
    def handler(request: httpx.Request) -> httpx.Response:
        raise remote_error

    client = TmpLinkClient("test-key", transport=httpx.MockTransport(handler))

    with pytest.raises(expected_error) as error:
        await client.quota()

    assert "test-key" not in str(error.value)
