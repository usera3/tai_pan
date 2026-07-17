from __future__ import annotations

from io import BytesIO
from typing import Any, BinaryIO

import httpx

from app.models import ServiceResult


DIRECT_URL = "https://tmp-api.vx-cdn.com/services/direct"
UPLOAD_URL = "https://tmp-cli.vx-cdn.com/app/upload_cli"
ALLOWED_STORAGE_MODELS = {0, 1, 2, 99}

STATUS_MESSAGES = {
    "0": "Remote service rejected the request",
    "2": "File is too large",
    "3": "Remote service is busy",
    "4": "私有空间不足，请改用 24 小时、3 天或 7 天的临时保存期限",
    "5": "Account quota is insufficient",
    "6": "API Key invalid",
    "1001": "File does not exist",
    "1002": "Remote service internal error",
    "1003": "Direct link does not exist",
    "1005": "File is not ready",
}


class TmpLinkError(RuntimeError):
    pass


class TmpLinkBusinessError(TmpLinkError):
    pass


class TmpLinkTimeoutError(TmpLinkError):
    pass


class TmpLinkConnectionError(TmpLinkError):
    pass


class TmpLinkClient:
    def __init__(
        self,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self._api_key = api_key
        self.transport = transport
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"{type(self).__name__}(timeout={self.timeout!r})"

    async def quota(self) -> ServiceResult:
        return await self._direct("quota")

    async def list_files(self, page: int = 1) -> ServiceResult:
        return await self._direct("list_of_workspace", empty_is_success=True, page=page)

    async def list_links(self, page: int = 1) -> ServiceResult:
        return await self._direct("list_of_direct", empty_is_success=True, page=page)

    async def create_link(
        self,
        ukey: str,
        valid_time: int | None = None,
        download_limit: int | None = None,
    ) -> ServiceResult:
        return await self._direct(
            "link_add",
            ukey=ukey,
            valid_time=valid_time,
            download_limit=download_limit,
        )

    async def create_download_link(self, ukey: str) -> ServiceResult:
        result = await self.create_link(ukey, valid_time=1440)
        data = result.data[0] if isinstance(result.data, list) and result.data else result.data
        if isinstance(data, dict) and data.get("link"):
            return ServiceResult(ok=result.ok, data=data, message=result.message)

        dkey = self._extract_dkey(data)
        if dkey:
            links = await self.list_links(page=1)
            for link in links.data if isinstance(links.data, list) else []:
                if isinstance(link, dict) and link.get("dkey") == dkey and link.get("link"):
                    return ServiceResult(ok=True, data=link, message=result.message)

        raise TmpLinkBusinessError("直链已创建，但钛盘未返回可用的下载地址")

    async def delete_link(self, dkey: str, delete_file: bool = False) -> ServiceResult:
        return await self._direct(
            "link_del",
            dkey=dkey,
            delete="1" if delete_file else "0",
        )

    async def delete_file(self, ukey: str) -> ServiceResult:
        created = await self.create_link(ukey)
        dkey = self._extract_dkey(created.data)
        if not dkey:
            raise TmpLinkBusinessError("钛盘未返回删除文件所需的 DKEY")
        return await self.delete_link(dkey, delete_file=True)

    async def upload(
        self,
        file_name: str,
        content: bytes,
        model: int,
    ) -> ServiceResult:
        return await self.upload_file(
            file_name,
            BytesIO(content),
            model,
            "application/octet-stream",
        )

    async def upload_file(
        self,
        file_name: str,
        file: BinaryIO,
        model: int,
        content_type: str,
    ) -> ServiceResult:
        if model not in ALLOWED_STORAGE_MODELS:
            raise ValueError(f"storage model must be one of {sorted(ALLOWED_STORAGE_MODELS)}")
        return await self._request(
            UPLOAD_URL,
            data={"key": self._api_key, "model": str(model)},
            files={"file": (file_name, file, content_type)},
        )

    async def _direct(
        self,
        action: str,
        *,
        empty_is_success: bool = False,
        **fields: Any,
    ) -> ServiceResult:
        form = {"action": action, "key": self._api_key}
        form.update(
            {
                key: str(value)
                for key, value in fields.items()
                if value is not None and value != ""
            }
        )
        return await self._request(
            DIRECT_URL,
            data=form,
            empty_is_success=empty_is_success,
        )

    async def _request(
        self,
        url: str,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes | BinaryIO, str]] | None = None,
        empty_is_success: bool = False,
    ) -> ServiceResult:
        translated_error: TmpLinkError | None = None
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout,
            ) as client:
                response = await client.post(url, data=data, files=files)
                response.raise_for_status()
        except httpx.TimeoutException:
            translated_error = TmpLinkTimeoutError("Remote service timed out")
        except (httpx.ConnectError, httpx.NetworkError):
            translated_error = TmpLinkConnectionError(
                "Unable to connect to remote service"
            )
        except httpx.HTTPStatusError as exc:
            translated_error = TmpLinkConnectionError(
                f"Remote service returned HTTP {exc.response.status_code}"
            )

        if translated_error is not None:
            raise translated_error

        try:
            payload = response.json()
        except ValueError as exc:
            raise TmpLinkBusinessError("Remote service returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise TmpLinkBusinessError("Remote service returned an invalid response")

        status = str(payload.get("status", "0"))
        if status == "0" and empty_is_success:
            return ServiceResult(ok=True, data=[], message="")
        if status != "1":
            raise TmpLinkBusinessError(self._message(status))
        return ServiceResult(ok=True, data=payload.get("data"), message="")

    @staticmethod
    def _message(status: str) -> str:
        return STATUS_MESSAGES.get(status, "Remote service rejected the request")

    @staticmethod
    def _extract_dkey(data: Any) -> str:
        if isinstance(data, str):
            return data.strip()
        if isinstance(data, list):
            for item in data:
                dkey = TmpLinkClient._extract_dkey(item)
                if dkey:
                    return dkey
        if isinstance(data, dict):
            for key in ("dkey", "direct_key"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""
