import asyncio

import httpx


_AUTHENTICATION_ERROR_MESSAGE = (
    "XZKB 专用账号登录失败，知识已本地保存，等待同步"
)


class XzkbAuthenticationError(ValueError):
    pass


class XzkbLocalAccountAuth:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._login_url = (
            f"{base_url.rstrip('/')}/kb-matrix-base/v1/auth/login-local"
        )
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(timeout=timeout)
        self._cached_token: str | None = None
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        if self._cached_token is not None:
            return self._cached_token
        async with self._lock:
            if self._cached_token is None:
                self._cached_token = await self._login()
            return self._cached_token

    async def invalidate(self, token: str) -> None:
        async with self._lock:
            if token == self._cached_token:
                self._cached_token = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self) -> str:
        try:
            response = await self._client.post(
                self._login_url,
                json={
                    "username": self._username,
                    "password": self._password,
                },
            )
            if response.status_code != 200:
                raise XzkbAuthenticationError(_AUTHENTICATION_ERROR_MESSAGE)
            payload = response.json()
        except httpx.HTTPError:
            raise XzkbAuthenticationError(
                _AUTHENTICATION_ERROR_MESSAGE
            ) from None
        except ValueError:
            raise XzkbAuthenticationError(
                _AUTHENTICATION_ERROR_MESSAGE
            ) from None

        if not isinstance(payload, dict) or str(payload.get("code")) != "200":
            raise XzkbAuthenticationError(_AUTHENTICATION_ERROR_MESSAGE)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise XzkbAuthenticationError(_AUTHENTICATION_ERROR_MESSAGE)
        access_token = data.get("accessToken")
        if not isinstance(access_token, str) or not access_token.strip():
            raise XzkbAuthenticationError(_AUTHENTICATION_ERROR_MESSAGE)
        return access_token.strip()
