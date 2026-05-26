from __future__ import annotations
import base64
import binascii
import re
import qbittorrentapi
from requests import Response
from .config import get_settings


class TorrentAlreadyExistsError(RuntimeError):
    def __init__(self, *, torrent_hash: str | None, torrent_name: str | None, save_path: str | None) -> None:
        self.torrent_hash = torrent_hash
        self.torrent_name = torrent_name
        self.save_path = save_path
        details = torrent_name or "unknown torrent"
        if torrent_hash:
            details = f"{details} ({torrent_hash})"
        if save_path:
            details = f"{details} at {save_path}"
        super().__init__(f"torrent already exists in qBittorrent: {details}")


class QbtService:
    _BTIH_PATTERN = re.compile(r"(^|[?&])xt=urn:btih:([A-Za-z0-9]{32}|[A-Fa-f0-9]{40})($|&)")
    _LOGIN_SUCCESS_STATUSES = {200, 204}
    _AUTH_COOKIE_NAMES = {"SID", "QBT_SID"}

    def __init__(self) -> None:
        self.settings = get_settings()

    def client(self) -> qbittorrentapi.Client:
        client = qbittorrentapi.Client(
            host=self.settings.qbt_host,
            username=self.settings.qbt_username,
            password=self.settings.qbt_password,
            VERIFY_WEBUI_CERTIFICATE=self.settings.qbt_verify_certificate,
            REQUESTS_ARGS={"timeout": self.settings.qbt_request_timeout_seconds},
        )
        try:
            self._log_in(client)
        except Exception as exc:
            raise RuntimeError(
                "qBittorrent login failed "
                f"(host={self.settings.qbt_host}, user={self.settings.qbt_username}): {self._format_exc(exc)}"
            ) from exc
        return client

    def _log_in(self, client: qbittorrentapi.Client) -> str:
        try:
            login_response = self._post_auth_login(client)
        except Exception as exc:
            cookie_names = self._qbt_session_cookie_names(client)
            raise RuntimeError(
                "auth/login request failed "
                f"(session_cookies={self._format_names(cookie_names)}): {self._format_exc(exc)}"
            ) from exc

        status_ok = login_response.status_code in self._LOGIN_SUCCESS_STATUSES
        body = (login_response.text or "").strip()
        body_ok = body == "Ok."
        cookie_names = self._qbt_session_cookie_names(client)

        version_response: Response | None = None
        version_error: Exception | None = None
        if status_ok:
            try:
                version_response = self._get_app_version_response(client)
            except Exception as exc:
                version_error = exc

        version_ok = version_response is not None and version_response.status_code == 200
        if status_ok and version_ok:
            return (version_response.text or "").strip()

        login_details = self._format_response(login_response)
        if not status_ok:
            raise RuntimeError(
                "auth/login returned an unexpected HTTP status "
                f"({login_details}, session_cookies={self._format_names(cookie_names)})"
            )

        if version_error is not None:
            response = getattr(version_error, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in (401, 403) or not cookie_names:
                raise RuntimeError(
                    "auth/login did not produce a usable authenticated session "
                    f"({login_details}, session_cookies={self._format_names(cookie_names)}, "
                    f"app_version_check={self._format_exc(version_error)})"
                ) from version_error
            raise RuntimeError(
                "auth/login returned a compatible response but app/version verification failed "
                f"({login_details}, session_cookies={self._format_names(cookie_names)}, "
                f"app_version_check={self._format_exc(version_error)})"
            ) from version_error

        version_details = self._format_response(version_response) if version_response is not None else "not attempted"
        success_markers = []
        if body_ok:
            success_markers.append("body=Ok.")
        if cookie_names:
            success_markers.append(f"session_cookies={self._format_names(cookie_names)}")
        raise RuntimeError(
            "auth/login did not produce a verified qBittorrent session "
            f"({login_details}, markers={self._format_names(success_markers)}, "
            f"app_version_check={version_details})"
        )

    def _post_auth_login(self, client: qbittorrentapi.Client) -> Response:
        client._initialize_context()
        return client._request(
            http_method="post",
            api_namespace="auth",
            api_method="login",
            data={"username": self.settings.qbt_username, "password": self.settings.qbt_password},
            response_class=Response,
        )

    @staticmethod
    def _get_app_version_response(client: qbittorrentapi.Client) -> Response:
        return client._request(
            http_method="get",
            api_namespace="app",
            api_method="version",
            response_class=Response,
        )

    @classmethod
    def _format_exc(cls, exc: Exception) -> str:
        message = str(exc).strip()
        if message:
            formatted = f"{exc.__class__.__name__}: {message}"
        else:
            formatted = repr(exc)

        response = getattr(exc, "response", None)
        if response is not None:
            formatted = f"{formatted} ({cls._format_response(response)})"
        return formatted

    @classmethod
    def _format_response(cls, response: Response | None) -> str:
        if response is None:
            return "no response"

        status = f"status={response.status_code}"
        if response.reason:
            status = f"{status} {response.reason}"

        body = (response.text or "").strip()
        body_detail = "body=<empty>" if not body else f"body={cls._truncate(body)!r}"
        set_cookie_names = cls._cookie_names(getattr(response, "cookies", None))
        if set_cookie_names:
            return f"{status}, {body_detail}, set_cookie_names={cls._format_names(set_cookie_names)}"
        return f"{status}, {body_detail}"

    @classmethod
    def _qbt_session_cookie_names(cls, client: qbittorrentapi.Client) -> list[str]:
        session = getattr(client, "_http_session", None)
        names = cls._cookie_names(getattr(session, "cookies", None))
        return [name for name in names if cls._is_qbt_session_cookie_name(name)]

    @classmethod
    def _is_qbt_session_cookie_name(cls, name: str) -> bool:
        return name in cls._AUTH_COOKIE_NAMES or name.startswith("QBT_SID_")

    @staticmethod
    def _cookie_names(cookie_jar) -> list[str]:
        if cookie_jar is None:
            return []

        names: set[str] = set()
        try:
            for cookie in cookie_jar:
                name = getattr(cookie, "name", None)
                if name:
                    names.add(str(name))
        except TypeError:
            pass

        if not names and hasattr(cookie_jar, "keys"):
            names.update(str(name) for name in cookie_jar.keys())
        return sorted(names)

    @staticmethod
    def _format_names(names: list[str]) -> str:
        return ", ".join(names) if names else "none"

    @staticmethod
    def _truncate(value: str, limit: int = 300) -> str:
        compact = " ".join(value.split())
        if len(compact) <= limit:
            return compact
        return f"{compact[:limit]}..."

    def add_torrent(self, magnet_uri: str, save_path: str, tags: list[str], category: str) -> None:
        client = self.client()
        infohash = self._extract_btih_hash(magnet_uri)
        existing = self._get_torrent_with_client(client, infohash) if infohash else None
        if existing is not None:
            raise TorrentAlreadyExistsError(
                torrent_hash=getattr(existing, "hash", None),
                torrent_name=getattr(existing, "name", None),
                save_path=getattr(existing, "save_path", None),
            )
        try:
            result = client.torrents_add(
                urls=magnet_uri,
                save_path=save_path,
                tags=tags,
                category=category,
                is_paused=False,
            )
            if isinstance(result, str) and result.strip().lower() != "ok.":
                existing = self._get_torrent_with_client(client, infohash) if infohash else None
                if existing is not None:
                    raise TorrentAlreadyExistsError(
                        torrent_hash=getattr(existing, "hash", None),
                        torrent_name=getattr(existing, "name", None),
                        save_path=getattr(existing, "save_path", None),
                    )
                raise RuntimeError(
                    f"unexpected qBittorrent add result: {result!r} "
                    "(generic qB add failure; often duplicate torrent, malformed magnet, or rejected save path/category)"
                )
        except TorrentAlreadyExistsError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "qBittorrent rejected torrent add request "
                f"(save_path={save_path}, category={category}): {self._format_exc(exc)}"
            ) from exc

    def find_existing_from_magnet(self, magnet_uri: str):
        client = self.client()
        infohash = self._extract_btih_hash(magnet_uri)
        if not infohash:
            return None
        return self._get_torrent_with_client(client, infohash)

    def find_by_unique_tag(self, unique_tag: str):
        client = self.client()
        torrents = client.torrents_info()
        for torrent in torrents:
            torrent_tags = getattr(torrent, "tags", "") or ""
            tags = {t.strip() for t in torrent_tags.split(",") if t.strip()}
            if unique_tag in tags:
                return torrent
        return None

    def get_torrent(self, torrent_hash: str):
        return self._get_torrent_with_client(self.client(), torrent_hash)

    def get_torrents(self, torrent_hashes: list[str]):
        hashes = [torrent_hash for torrent_hash in dict.fromkeys(torrent_hashes) if torrent_hash]
        if not hashes:
            return []
        return list(self.client().torrents_info(torrent_hashes="|".join(hashes)))

    def list_torrents(self):
        return list(self.client().torrents_info())

    def transfer_info(self) -> dict[str, object]:
        response = self.client()._request(
            http_method="get",
            api_namespace="transfer",
            api_method="info",
            response_class=Response,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"invalid qBittorrent transfer info response: {self._format_response(response)}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"unexpected qBittorrent transfer info payload: {payload!r}")
        return payload

    def pause(self, torrent_hash: str) -> None:
        self.client().torrents_pause(torrent_hashes=torrent_hash)

    def resume(self, torrent_hash: str) -> None:
        self.client().torrents_resume(torrent_hashes=torrent_hash)

    def delete_with_files(self, torrent_hash: str) -> None:
        self.client().torrents_delete(torrent_hashes=torrent_hash, delete_files=True)

    def set_location(self, torrent_hash: str, location: str) -> None:
        self.client().torrents_set_location(torrent_hashes=torrent_hash, location=location)

    def set_category(self, torrent_hash: str, category: str) -> None:
        self.client().torrents_set_category(torrent_hashes=torrent_hash, category=category)

    def set_save_path(self, torrent_hash: str, save_path: str) -> None:
        self.client().torrents_set_save_path(torrent_hashes=torrent_hash, save_path=save_path)

    def list_categories(self) -> list[str]:
        categories = self.client().torrents_categories()
        if hasattr(categories, "keys"):
            return sorted(str(name) for name in categories.keys())
        return []

    def list_save_path_suggestions(self) -> list[str]:
        client = self.client()
        paths: set[str] = set()
        for torrent in client.torrents_info():
            path = getattr(torrent, "save_path", None)
            if isinstance(path, str) and path.strip():
                paths.add(path.strip())
        categories = client.torrents_categories()
        if hasattr(categories, "values"):
            for info in categories.values():
                path = getattr(info, "save_path", None) or getattr(info, "savePath", None)
                if isinstance(path, str) and path.strip():
                    paths.add(path.strip())
        return sorted(paths)

    def resolve_or_create_category(self, category: str, *, create_if_missing: bool) -> str:
        requested = category.strip()
        if not requested:
            raise RuntimeError("final category is empty")

        categories = self.client().torrents_categories()
        existing = {str(name): str(name) for name in categories.keys()}
        exact = existing.get(requested)
        if exact:
            return exact

        lower_map = {name.lower(): name for name in existing}
        case_match = lower_map.get(requested.lower())
        if case_match:
            return case_match

        if not create_if_missing:
            raise RuntimeError(
                f"final category '{requested}' not found in qBittorrent; "
                "enable TI_AUTO_CREATE_FINAL_CATEGORY or create it in qBittorrent first"
            )

        try:
            self.client().torrents_create_category(name=requested)
        except Exception as exc:
            raise RuntimeError(
                f"failed to create qBittorrent category '{requested}': {self._format_exc(exc)}"
            ) from exc
        return requested

    def _get_torrent_with_client(self, client: qbittorrentapi.Client, torrent_hash: str | None):
        if not torrent_hash:
            return None
        torrents = client.torrents_info(torrent_hashes=torrent_hash)
        if not torrents:
            return None
        return torrents[0]

    def _extract_btih_hash(self, magnet_uri: str) -> str | None:
        match = self._BTIH_PATTERN.search(magnet_uri)
        if not match:
            return None
        raw_hash = match.group(2).strip()
        if len(raw_hash) == 40:
            return raw_hash.lower()
        try:
            decoded = base64.b32decode(raw_hash.upper())
            return decoded.hex().lower()
        except (binascii.Error, ValueError):
            return None
