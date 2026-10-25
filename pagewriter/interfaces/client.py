"""Standard-library client; ignores environment proxies and refuses redirects."""
import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .common import InterfaceError, failure, parse_json
from .discovery import read_connection


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ApiClient:
    def __init__(self, connection_file, timeout=35):
        self.connection_file = connection_file
        self.timeout = timeout

    def call(self, method, params=None):
        data = read_connection(self.connection_file)
        raw = json.dumps({"method": method, "params": params or {}},
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(data["url"].rstrip("/") + "/v1/call", data=raw,
                          headers={"Authorization": "Bearer " + data["token"],
                                   "Content-Type": "application/json"}, method="POST")
        try:
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            try:
                response = opener.open(request, timeout=self.timeout)
            except HTTPError as exc:
                response = exc
            with response:
                content = response.read(16 * 1024 * 1024 + 1)
                if len(content) > 16 * 1024 * 1024:
                    raise InterfaceError("response_too_large", "Local API response exceeds the size limit.")
                envelope = parse_json(content)
                if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
                    raise ValueError("invalid envelope")
                return envelope
        except (URLError, TimeoutError, OSError):
            return failure("connection_error", "Cannot reach the local application service.")
        except (ValueError, UnicodeError):
            return failure("invalid_response", "Local API returned an invalid response.")
