import httpx

_client: httpx.AsyncClient | None = None

_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 10.0


def get_async_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT))
    return _client


def new_isolated_client(timeout: float) -> httpx.AsyncClient:
    """A caller-owned client for one slow side call, e.g. a web-grounded lookup.

    Kept out of the shared pool so a request measured in tens of seconds neither
    fights the hot path's read budget nor recycles its connections when it fails.
    The caller closes it — use it as an ``async with``.
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=_CONNECT_TIMEOUT))


async def close_async_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
