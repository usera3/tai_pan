from io import BytesIO
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
async def test_upload_file_streams_a_file_object_in_bounded_reads():
    class BoundedReadFile(BytesIO):
        def __init__(self, content: bytes):
            super().__init__(content)
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            if size < 0:
                raise AssertionError("stream must not be read without a bound")
            return super().read(size)

    class ObservingTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.reads_before_send: list[int] | None = None
            self.body = b""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.reads_before_send = list(source.read_sizes)
            self.body = await request.aread()
            return httpx.Response(
                200,
                json={"status": 1, "data": "STREAMED-UKEY"},
                request=request,
            )

    source = BoundedReadFile(b"streamed-content")
    transport = ObservingTransport()
    client = TmpLinkClient("stream-key", transport=transport)

    result = await client.upload_file(
        "report.txt",
        source,
        model=2,
        content_type="text/plain",
    )

    assert transport.reads_before_send == []
    assert b"streamed-content" in transport.body
    assert source.read_sizes
    assert all(0 < size <= 64 * 1024 for size in source.read_sizes)
    assert result.data == "STREAMED-UKEY"


def test_client_keeps_api_key_private_and_has_an_explicit_safe_repr():
    api_key = "private-key-must-not-render"
    client = TmpLinkClient(api_key, transport=httpx.MockTransport(lambda request: None))

    assert not hasattr(client, "api_key")
    assert "_api_key" in vars(client)
    assert api_key not in repr(client)
    assert repr(client).startswith("TmpLinkClient(")


@pytest.mark.asyncio
async def test_upload_rejects_unknown_storage_model():
    client = TmpLinkClient("test-key", transport=httpx.MockTransport(lambda request: None))

    with pytest.raises(ValueError, match="storage model"):
        await client.upload("report.txt", b"hello", model=5)


@pytest.mark.asyncio
async def test_empty_remote_lists_are_successful_empty_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 0, "data": False, "debug": []})

    client = TmpLinkClient("test-key", transport=httpx.MockTransport(handler))

    files = await client.list_files()
    links = await client.list_links()

    assert files.ok is True
    assert files.data == []
    assert links.ok is True
    assert links.data == []


@pytest.mark.asyncio
async def test_permanent_upload_space_error_explains_the_next_action():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 4, "data": False})

    client = TmpLinkClient("test-key", transport=httpx.MockTransport(handler))

    with pytest.raises(TmpLinkBusinessError, match="私有空间不足.*临时保存期限"):
        await client.upload("report.txt", b"hello", model=99)


@pytest.mark.asyncio
async def test_download_link_is_valid_for_one_day_without_download_limit():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 2:
            return httpx.Response(
                200,
                json={
                    "status": 1,
                    "data": [{"dkey": "D1", "link": "https://files.example/D1"}],
                },
            )
        return httpx.Response(
            200,
            json={"status": 1, "data": [{"dkey": "D1"}]},
        )

    client = TmpLinkClient("test-key", transport=httpx.MockTransport(handler))

    result = await client.create_download_link("FILE-UKEY")

    assert form_data(captured[0]) == {
        "action": "link_add",
        "key": "test-key",
        "ukey": "FILE-UKEY",
        "valid_time": "1440",
    }
    assert form_data(captured[1]) == {
        "action": "list_of_direct",
        "key": "test-key",
        "page": "1",
    }
    assert result.data == {"dkey": "D1", "link": "https://files.example/D1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "created_data",
    ["D1", {"dkey": "D1"}, {"direct_key": "D1"}, [{"dkey": "D1"}]],
)
async def test_delete_file_creates_link_then_deletes_source(created_data):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 1:
            return httpx.Response(200, json={"status": 1, "data": created_data})
        return httpx.Response(200, json={"status": 1, "data": True})

    client = TmpLinkClient("test-key", transport=httpx.MockTransport(handler))

    await client.delete_file("FILE-UKEY")

    assert form_data(captured[0]) == {
        "action": "link_add",
        "key": "test-key",
        "ukey": "FILE-UKEY",
    }
    assert form_data(captured[1]) == {
        "action": "link_del",
        "key": "test-key",
        "dkey": "D1",
        "delete": "1",
    }


@pytest.mark.asyncio
async def test_delete_file_stops_when_link_response_has_no_dkey():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"status": 1, "data": {"link": "/d/example"}})

    client = TmpLinkClient("test-key", transport=httpx.MockTransport(handler))

    with pytest.raises(TmpLinkBusinessError, match="DKEY"):
        await client.delete_file("FILE-UKEY")

    assert len(captured) == 1


@pytest.mark.asyncio
async def test_business_error_is_descriptive_and_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 6, "message": "API Key invalid"})

    client = TmpLinkClient("test-key", transport=httpx.MockTransport(handler))

    with pytest.raises(TmpLinkBusinessError, match="API Key invalid") as error:
        await client.quota()

    assert "test-key" not in str(error.value)


@pytest.mark.asyncio
async def test_business_error_ignores_a_remote_message_that_echoes_the_key():
    api_key = "key-that-the-remote-must-not-echo"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": 6, "message": f"invalid credential: {api_key}"},
        )

    client = TmpLinkClient(api_key, transport=httpx.MockTransport(handler))

    with pytest.raises(TmpLinkBusinessError) as captured:
        await client.quota()

    rendered = f"{captured.value!r} {captured.value} {client!r}"
    assert api_key not in rendered


@pytest.mark.asyncio
async def test_success_response_does_not_preserve_a_remote_message():
    api_key = "key-that-the-remote-must-not-echo"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": 1, "data": {"ok": True}, "message": api_key},
        )

    client = TmpLinkClient(api_key, transport=httpx.MockTransport(handler))

    result = await client.quota()

    assert result.message == ""
    assert api_key not in repr(result)


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
    assert error.value.__context__ is None
    assert error.value.__cause__ is None


@pytest.mark.asyncio
async def test_http_status_translation_drops_the_httpx_exception_chain():
    api_key = "request-body-secret-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    client = TmpLinkClient(api_key, transport=httpx.MockTransport(handler))

    with pytest.raises(TmpLinkConnectionError) as captured:
        await client.quota()

    assert captured.value.__context__ is None
    assert captured.value.__cause__ is None
    assert api_key not in repr(captured.value)
