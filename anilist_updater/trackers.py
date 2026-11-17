"""Secondary tracker synchronization (MyAnimeList and Shikimori) with OAuth refresh and split-cour support."""

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .cache import _write_json
from .models import AnimeInfo, Status

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
MAL_AUTH_PATH = ROOT_DIR / "mal_auth.json"
SHIKI_AUTH_PATH = ROOT_DIR / "shiki_auth.json"
TIMEOUT = 10
USER_AGENT = "mpv-anilist-updater"

MAL_STATUS_MAP = {
    Status.CURRENT: "watching",
    Status.PLANNING: "plan_to_watch",
    Status.COMPLETED: "completed",
    Status.DROPPED: "dropped",
    Status.PAUSED: "on_hold",
    Status.REPEATING: "completed",
}

SHIKI_STATUS_MAP = {
    Status.CURRENT: "watching",
    Status.PLANNING: "planned",
    Status.COMPLETED: "completed",
    Status.DROPPED: "dropped",
    Status.PAUSED: "on_hold",
    Status.REPEATING: "rewatching",
}

_POINT_3_SCORES = {1: 4, 2: 6, 3: 8}


def _today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


def score_to_ten(score: float | None, score_format: str | None) -> int | None:
    """Convert an AniList score in `score_format` to an integer 0-10 score for MAL and Shikimori."""
    if score is None:
        return None
    if score <= 0:
        return 0
    if score_format == "POINT_3":
        return _POINT_3_SCORES.get(int(score), 0)
    scales = {
        "POINT_100": score / 10,
        "POINT_10": score,
        "POINT_10_DECIMAL": score,
        "POINT_5": score * 2,
    }
    scaled = scales.get(score_format or "", score if score <= 10 else score / 10)
    return round(scaled)


@dataclass(frozen=True, slots=True)
class TrackerUpdate:
    """Normalized sync payload computed from AniList `AnimeInfo` and transition state."""

    mal_id: int
    status: Status | None = None
    progress: int | None = None
    is_rewatching: bool | None = None
    set_start_date: bool = False
    set_finish_date: bool = False
    increment_rewatch_count: bool = False
    score: int | None = None


def build_tracker_update(
    info: AnimeInfo,
    target_status: Status | None,
    prev_status: Status | None,
    progress: int | None,
    *,
    score: int | None = None,
) -> TrackerUpdate:
    """Build a MAL/Shikimori update, respecting AniList split-cours sharing the same `mal_id`."""
    assert info.mal_id is not None
    effective_status = (
        Status.CURRENT if (info.has_mal_sequel and target_status == Status.COMPLETED) else target_status
    )
    effective_progress = (progress + info.mal_offset) if progress is not None else None
    is_rewatching = (
        True
        if effective_status == Status.REPEATING
        else (False if prev_status == Status.REPEATING and effective_status == Status.COMPLETED else None)
    )
    set_start = effective_status in {Status.CURRENT, Status.COMPLETED} and prev_status not in {
        Status.CURRENT,
        Status.REPEATING,
        Status.COMPLETED,
    }
    set_finish = effective_status == Status.COMPLETED and prev_status not in {Status.REPEATING, Status.COMPLETED}
    inc_rewatches = prev_status == Status.REPEATING and effective_status == Status.COMPLETED
    return TrackerUpdate(
        mal_id=info.mal_id,
        status=effective_status,
        progress=effective_progress,
        is_rewatching=is_rewatching,
        set_start_date=set_start,
        set_finish_date=set_finish,
        increment_rewatch_count=inc_rewatches,
        score=score,
    )


class _OAuthTracker:
    """Shared JSON auth storage, OAuth token refresh and warning state for secondary trackers."""

    label: str = ""
    setup_script: str = ""
    oauth_url: str = ""
    base_url: str = ""
    requires_client_secret: bool = False
    requires_user_id: bool = False

    def __init__(self, auth_path: Path) -> None:
        """Bind credential file path and per-run warning collector."""
        self.auth_path = auth_path
        self._access_token: str | None = None
        self._user_id: int | None = None
        self._warned: bool = False
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        """Record a deduplicated OSD warning message."""
        if message not in self.warnings:
            self.warnings.append(message)

    def _warn_auth(self, message: str | None = None) -> None:
        if not self._warned:
            self._warned = True
            self.warn(message or f"{self.label}: Authorization expired. Run {self.setup_script}")

    def _read_auth(self) -> dict[str, Any] | None:
        if not self.auth_path.exists():
            return None
        try:
            data = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("Error reading %s auth: %s", self.label, e)
            return None
        return data if isinstance(data, dict) else None

    def _write_auth(self, auth_data: dict[str, Any]) -> bool:
        try:
            _write_json(self.auth_path, auth_data)
            return True
        except OSError as e:
            log.warning("Error writing %s auth: %s", self.label, e)
            return False

    def _set_auth_error(self, reason: str = "") -> None:
        if auth_data := self._read_auth():
            auth_data["auth_error"] = True
            if reason:
                auth_data["auth_error_reason"] = reason
            self._write_auth(auth_data)

    def _refresh_payload(self, auth_data: dict[str, Any]) -> dict[str, str] | None:
        client_id = auth_data.get("client_id")
        refresh_token = auth_data.get("refresh_token")
        client_secret = auth_data.get("client_secret")
        if not client_id or not refresh_token or (self.requires_client_secret and not client_secret):
            self._set_auth_error("Missing credentials")
            self._warn_auth(f"{self.label}: Incomplete credentials. Run {self.setup_script}")
            return None
        payload = {"client_id": str(client_id), "grant_type": "refresh_token", "refresh_token": str(refresh_token)}
        if self.requires_client_secret:
            payload["client_secret"] = str(client_secret)
        return payload

    def refresh_access_token(self) -> bool:
        """Refresh OAuth access token using the stored refresh token."""
        auth_data = self._read_auth()
        if not auth_data:
            return False
        payload = self._refresh_payload(auth_data)
        if not payload:
            return False

        log.info("%s access token expired. Attempting to refresh...", self.label)
        body = urllib.parse.urlencode(payload).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT}
        request = urllib.request.Request(self.oauth_url, body, headers)  # ruff: ignore[suspicious-url-open-usage]

        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # ruff: ignore[suspicious-url-open-usage]
                token_data = json.load(response)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            log.warning("Failed to refresh %s token: %s - %s", self.label, e.code, err_body)
            if e.code in {400, 401}:
                self._set_auth_error(f"Refresh rejected ({e.code})")
                self._warn_auth()
            return False
        except (OSError, http.client.HTTPException, ValueError) as e:
            log.warning("Error during %s token refresh: %s", self.label, e)
            return False

        auth_data["access_token"] = token_data["access_token"]
        auth_data["refresh_token"] = token_data["refresh_token"]
        if "expires_in" in token_data:
            auth_data["expires_at"] = int(time.time()) + int(token_data["expires_in"])
        auth_data.pop("auth_error", None)
        auth_data.pop("auth_error_reason", None)
        if not self._write_auth(auth_data):
            return False
        self._access_token = token_data["access_token"]
        log.info("%s token refreshed successfully!", self.label)
        return True

    def check_auth(self) -> None:
        """Check auth file state at startup / `info`; proactively refresh an expired token."""
        auth_data = self._read_auth()
        if not auth_data:
            return
        if auth_data.get("auth_error"):
            self._warn_auth()
            return
        expires_at = auth_data.get("expires_at")
        if expires_at is not None and time.time() >= expires_at - 60:
            self.refresh_access_token()
            return
        missing_token = not auth_data.get("access_token")
        missing_uid = self.requires_user_id and not auth_data.get("user_id")
        if missing_token or missing_uid:
            self._warn_auth(f"{self.label}: Incomplete credentials. Run {self.setup_script}")

    def _ensure_credentials(self) -> bool:
        auth_data = self._read_auth()
        if not auth_data:
            return False
        if auth_data.get("auth_error"):
            self._warn_auth()
            return False
        expires_at = auth_data.get("expires_at")
        if expires_at is not None and time.time() >= expires_at - 60:
            if not self.refresh_access_token():
                return False
            auth_data = self._read_auth() or {}
        if not self._access_token or (self.requires_user_id and not self._user_id):
            token = auth_data.get("access_token")
            user_id = auth_data.get("user_id")
            if not token or (self.requires_user_id and not user_id):
                self._warn_auth(f"{self.label}: Incomplete credentials. Run {self.setup_script}")
                return False
            self._access_token = str(token)
            self._user_id = int(user_id) if user_id is not None else None
        return True

    def _send_http(
        self, request: urllib.request.Request, endpoint: str, *, allow_retry: bool
    ) -> dict[str, Any] | list[Any] | None:
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # ruff: ignore[suspicious-url-open-usage]
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            if e.code in {401, 403}:
                if allow_retry and self.refresh_access_token():
                    return None  # sentinel handled by _request
                self._set_auth_error(f"API {e.code}" if allow_retry else f"API {e.code} on retry")
                msg = None if allow_retry else f"{self.label}: Authorization failed. Run {self.setup_script}"
                self._warn_auth(msg)
                return None
            log.warning("%s API request failed: %s - %s (%s)", self.label, e.code, err_body, endpoint)
            return None
        except (OSError, http.client.HTTPException, ValueError) as e:
            log.warning("%s request error (%s): %s", self.label, endpoint, e)
            return None

    def _request(
        self,
        endpoint: str,
        method: str = "GET",
        *,
        params: dict[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        for attempt in range(2):
            if not self._ensure_credentials():
                return None

            url = f"{self.base_url}/{endpoint}"
            if params:
                url += "?" + urllib.parse.urlencode(params)
            headers = {
                "Authorization": f"Bearer {self._access_token}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
            if content_type:
                headers["Content-Type"] = content_type

            request = urllib.request.Request(url, data=body, headers=headers, method=method)  # ruff: ignore[suspicious-url-open-usage]
            token_before = self._access_token
            result = self._send_http(request, endpoint, allow_retry=attempt == 0)
            if result is not None or self._access_token == token_before or self._warned:
                return result
        return None


class MalClient(_OAuthTracker):
    """MyAnimeList REST API v2 sync client."""

    label = "MyAnimeList"
    setup_script = "setup_auth.py"
    oauth_url = "https://myanimelist.net/v1/oauth2/token"
    base_url = "https://api.myanimelist.net/v2"

    def __init__(self, auth_path: Path = MAL_AUTH_PATH) -> None:
        """Create a MyAnimeList client backed by `auth_path`."""
        super().__init__(auth_path)

    def update(self, upd: TrackerUpdate) -> None:
        """Update episode progress, status, dates, rewatches and score on MyAnimeList."""
        if not self.auth_path.exists() or not self._ensure_credentials():
            return

        mal_status = MAL_STATUS_MAP.get(upd.status) if upd.status else None
        is_rewatching = True if upd.status == Status.REPEATING else upd.is_rewatching

        data: dict[str, Any] = {}
        if upd.progress is not None:
            data["num_watched_episodes"] = upd.progress
        if mal_status:
            data["status"] = mal_status
        if is_rewatching is not None:
            data["is_rewatching"] = "true" if is_rewatching else "false"
        if upd.set_start_date:
            data["start_date"] = _today_iso()
        if upd.set_finish_date:
            data["finish_date"] = _today_iso()
        if upd.score is not None:
            data["score"] = upd.score

        if upd.increment_rewatch_count:
            existing = self._request(f"anime/{upd.mal_id}", params={"fields": "my_list_status"})
            if isinstance(existing, dict) and "my_list_status" in existing:
                data["num_times_rewatched"] = existing["my_list_status"].get("num_times_rewatched", 0) + 1

        if not data:
            return

        body = urllib.parse.urlencode(data).encode()
        response = self._request(
            f"anime/{upd.mal_id}/my_list_status",
            method="PATCH",
            body=body,
            content_type="application/x-www-form-urlencoded",
        )
        if not isinstance(response, dict) or "num_episodes_watched" not in response:
            log.warning("Failed to update MAL entry %s.", upd.mal_id)
            if not self._warned:
                self.warn("MyAnimeList: Failed to update entry.")
        else:
            log.info("Successfully updated MAL entry %s.", upd.mal_id)


class ShikimoriClient(_OAuthTracker):
    """Shikimori REST API v2 sync client."""

    label = "Shikimori"
    setup_script = "setup_auth_shiki.py"
    oauth_url = "https://shikimori.io/oauth/token"
    base_url = "https://shikimori.io/api"
    requires_client_secret = True
    requires_user_id = True

    def __init__(self, auth_path: Path = SHIKI_AUTH_PATH) -> None:
        """Create a Shikimori client backed by `auth_path`."""
        super().__init__(auth_path)

    def update(self, upd: TrackerUpdate) -> None:
        """Update episode progress, status, rewatches and score on Shikimori."""
        if not self.auth_path.exists() or not self._ensure_credentials() or self._user_id is None:
            return

        shiki_status = SHIKI_STATUS_MAP.get(upd.status) if upd.status else None
        rates = self._request(
            "v2/user_rates",
            method="GET",
            params={"user_id": self._user_id, "target_id": upd.mal_id, "target_type": "Anime"},
        )
        if rates is None:
            if not self._warned:
                self.warn("Shikimori: Failed to fetch user rates.")
            return

        rate_id = None
        current_rewatches = 0
        current_status = None
        if isinstance(rates, list) and rates:
            rate = rates[0]
            rate_id = rate.get("id")
            current_rewatches = rate.get("rewatches", 0)
            current_status = rate.get("status")

        user_rate: dict[str, Any] = {}
        if upd.progress is not None:
            user_rate["episodes"] = upd.progress
        if shiki_status:
            user_rate["status"] = shiki_status
        if current_status == "rewatching" and shiki_status == "completed":
            user_rate["rewatches"] = current_rewatches + 1
        if upd.score is not None:
            user_rate["score"] = upd.score

        if not user_rate:
            return

        if rate_id is not None:
            endpoint, method = f"v2/user_rates/{rate_id}", "PATCH"
        else:
            endpoint, method = "v2/user_rates", "POST"
            user_rate.update({"user_id": self._user_id, "target_id": upd.mal_id, "target_type": "Anime"})

        body = json.dumps({"user_rate": user_rate}).encode()
        response = self._request(endpoint, method=method, body=body, content_type="application/json")
        if not isinstance(response, dict) or "id" not in response:
            log.warning("Failed to update Shikimori entry %s.", upd.mal_id)
            if not self._warned:
                self.warn("Shikimori: Failed to update entry.")
        else:
            log.info("Successfully updated Shikimori entry %s.", upd.mal_id)


class TrackerSync:
    """Coordinate optional MyAnimeList and Shikimori updates and collect OSD warnings."""

    def __init__(self, mal: MalClient | None = None, shiki: ShikimoriClient | None = None) -> None:
        """Bind optional MAL and Shikimori clients (defaulting to local credential files)."""
        self.mal = mal or MalClient()
        self.shiki = shiki or ShikimoriClient()

    def check_auth(self) -> list[str]:
        """Check configured secondary tracker tokens and return any user warnings."""
        self.mal.check_auth()
        self.shiki.check_auth()
        return self.take_warnings()

    def take_warnings(self) -> list[str]:
        """Return and clear accumulated tracker warnings."""
        warnings = [*self.mal.warnings, *self.shiki.warnings]
        self.mal.warnings.clear()
        self.shiki.warnings.clear()
        return warnings

    def sync_update(
        self,
        info: AnimeInfo,
        target_status: Status | None,
        prev_status: Status | None,
        progress: int | None,
    ) -> list[str]:
        """Sync an episode progress/status update to MAL and Shikimori."""
        if info.mal_id is None:
            return self.take_warnings()
        upd = build_tracker_update(info, target_status, prev_status, progress)
        self.mal.update(upd)
        self.shiki.update(upd)
        return self.take_warnings()

    def sync_correction(
        self,
        info: AnimeInfo,
        target_status: Status | None,
        prev_status: Status | None,
        score: float | None = None,
        score_format: str | None = None,
    ) -> list[str]:
        """Sync a manual status and/or score correction to MAL and Shikimori."""
        if info.mal_id is None:
            return self.take_warnings()
        upd = build_tracker_update(info, target_status, prev_status, None, score=score_to_ten(score, score_format))
        self.mal.update(upd)
        self.shiki.update(upd)
        return self.take_warnings()
