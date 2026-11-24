from django.conf import settings

from stellar_sdk import RequestsClient, Server


def get_horizon(*, client: RequestsClient | None = None) -> Server:
    server = Server(settings.HORIZON_URL, client=client)
    server._client.request_timeout = settings.DEFAULT_GET_TIMEOUT
    server._client._session.headers.update({'User-Agent': settings.HORIZON_REQUEST_USER_AGENT})
    return server
