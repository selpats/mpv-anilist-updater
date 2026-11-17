import io
import json
import urllib.error
import urllib.parse
import urllib.request

from fakes import FakeClient

from anilist_updater import cli
from anilist_updater.api import AniListClient
from anilist_updater.cache import Cache, Pending
from anilist_updater.errors import AniListError
from anilist_updater.models import AnimeInfo, Correction, FileInfo, Options, Status
from anilist_updater.resolve import refresh, resolve
from anilist_updater.service import Service
from anilist_updater.trackers import MalClient, ShikimoriClient, TrackerSync, score_to_ten


class _HttpResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _RecordingAniList(FakeClient):
    def __init__(self, *, score_fmt="POINT_10_DECIMAL", **kwargs):
        super().__init__(**kwargs)
        self.saves = []
        self._fmt = score_fmt
        self.down = False

    def related(self, ids):
        return {i: self.by_id[i] for i in ids if i in self.by_id}

    def score_format(self):
        return self._fmt

    def save_entry(self, anime_id, status=None, progress=None, score=None, dates=None):
        if self.down:
            raise AniListError("maintenance", 503, retryable=True)
        self.saves.append((anime_id, status, progress, score, dates))
        return {"status": status or "CURRENT", "progress": progress}


def _sbr_media():
    stage1 = {
        "id": 190327,
        "idMal": 61469,
        "title": {"romaji": "JoJo no Kimyou na Bouken: Steel Ball Run - 1st STAGE", "english": None},
        "episodes": 1,
        "duration": 47,
        "format": "TV",
        "status": "FINISHED",
        "startDate": {"year": 2026},
        "mediaListEntry": {"status": "COMPLETED", "progress": 1},
        "relations": {
            "edges": [
                {
                    "relationType": "PREQUEL",
                    "node": {"id": 146722, "idMal": 51367, "episodes": 14, "format": "TV"},
                },
                {
                    "relationType": "SEQUEL",
                    "node": {"id": 210482, "idMal": 61469, "episodes": 11, "format": "TV"},
                },
            ]
        },
    }
    stage2 = {
        "id": 210482,
        "idMal": 61469,
        "title": {"romaji": "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE", "english": None},
        "episodes": 11,
        "duration": 24,
        "format": "TV",
        "status": "RELEASING",
        "startDate": {"year": 2026},
        "mediaListEntry": {"status": "CURRENT", "progress": 1},
        "relations": {
            "edges": [
                {
                    "relationType": "PREQUEL",
                    "node": {"id": 190327, "idMal": 61469, "episodes": 1, "format": "TV"},
                }
            ]
        },
    }
    return stage1, stage2


def _write_auth(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_score_conversion_scales():
    assert score_to_ten(None, "POINT_100") is None
    assert score_to_ten(0, "POINT_100") == 0
    assert score_to_ten(85, "POINT_100") == 8 or score_to_ten(85, "POINT_100") == 9
    assert score_to_ten(86, "POINT_100") == 9
    assert score_to_ten(8.5, "POINT_10_DECIMAL") == 8
    assert score_to_ten(8.6, "POINT_10_DECIMAL") == 9
    assert score_to_ten(4.5, "POINT_5") == 9
    assert score_to_ten(1, "POINT_3") == 4
    assert score_to_ten(2, "POINT_3") == 6
    assert score_to_ten(3, "POINT_3") == 8


def test_missing_tracker_auth_files_produce_no_warnings_or_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no network"))
    )
    tracker = TrackerSync(MalClient(tmp_path / "mal.json"), ShikimoriClient(tmp_path / "shiki.json"))
    assert tracker.check_auth() == []
    info = AnimeInfo(anime_id=1, episode=1, mal_id=999)
    assert tracker.sync_update(info, Status.CURRENT, None, 1) == []


def test_auth_error_flags_warn_on_info_without_network(tmp_path, monkeypatch):
    mal_path = tmp_path / "mal_auth.json"
    shiki_path = tmp_path / "shiki_auth.json"
    _write_auth(mal_path, {"client_id": "cid", "access_token": "tok", "refresh_token": "ref", "auth_error": True})
    _write_auth(
        shiki_path,
        {
            "client_id": "cid",
            "client_secret": "sec",
            "access_token": "tok",
            "refresh_token": "ref",
            "user_id": 1,
            "auth_error": True,
        },
    )
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no network"))
    )

    tracker = TrackerSync(MalClient(mal_path), ShikimoriClient(shiki_path))
    warnings = tracker.check_auth()
    assert len(warnings) == 2
    assert "MyAnimeList" in warnings[0]
    assert "Shikimori" in warnings[1]


def test_expired_mal_token_refreshes_and_persists_new_tokens(tmp_path, monkeypatch):
    mal_path = tmp_path / "mal_auth.json"
    _write_auth(
        mal_path,
        {"client_id": "cid", "access_token": "old_tok", "refresh_token": "old_ref", "expires_at": 100},
    )

    calls = []

    def fake_urlopen(request, **_kwargs):
        calls.append((request.full_url, request.data.decode()))
        return _HttpResponse(
            json.dumps({"access_token": "new_tok", "refresh_token": "new_ref", "expires_in": 3600}).encode()
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    tracker = TrackerSync(MalClient(mal_path), ShikimoriClient(tmp_path / "missing_shiki.json"))
    assert tracker.check_auth() == []
    assert len(calls) == 1
    assert "refresh_token=old_ref" in calls[0][1]

    saved = json.loads(mal_path.read_text(encoding="utf-8"))
    assert (saved["access_token"], saved["refresh_token"]) == ("new_tok", "new_ref")
    assert "auth_error" not in saved


def test_shiki_401_triggers_refresh_and_retries_and_sets_auth_error_on_failure(tmp_path, monkeypatch):
    shiki_path = tmp_path / "shiki_auth.json"
    _write_auth(
        shiki_path,
        {
            "client_id": "cid",
            "client_secret": "sec",
            "access_token": "old",
            "refresh_token": "ref",
            "user_id": 655524,
        },
    )

    def failing_urlopen(request, **_kwargs):
        if "oauth/token" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"invalid_grant"))
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b"Forbidden"))

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    tracker = TrackerSync(MalClient(tmp_path / "missing_mal.json"), ShikimoriClient(shiki_path))
    warnings = tracker.sync_update(AnimeInfo(anime_id=1, episode=1, mal_id=61469), Status.CURRENT, None, 1)
    assert warnings == ["Shikimori: Authorization expired. Run setup_auth_shiki.py"]
    assert json.loads(shiki_path.read_text(encoding="utf-8"))["auth_error"] is True


def test_split_cour_steel_ball_run_resolves_caches_and_syncs_mal_and_shiki(tmp_path, monkeypatch):
    stage1, stage2 = _sbr_media()
    client = _RecordingAniList(
        user=[stage1, stage2],
        everything=[stage1, stage2],
        by_id={stage1["id"]: stage1, stage2["id"]: stage2},
    )

    info_s2 = resolve(client, FileInfo("JoJo no Kimyou na Bouken Steel Ball Run", 3))
    assert (info_s2.anime_id, info_s2.episode, info_s2.mal_id) == (210482, 2, 61469)
    assert (info_s2.mal_offset, info_s2.has_mal_sequel) == (1, False)

    # Cached round-trip retains split-cour fields
    cache = Cache(Options(), tmp_path / "cache.json")
    cache.put("/anime/SBR - 03.mkv", info_s2)
    cache.save()
    cached = Cache(Options(), tmp_path / "cache.json").get("/anime/SBR - 03.mkv", info_s2.guessed_name).info_for(3)
    assert (cached.episode, cached.mal_offset, cached.has_mal_sequel) == (2, 1, False)

    # First stage sees sequel with identical idMal -> holds MAL/Shikimori status at CURRENT
    stage1_only = dict(stage1, mediaListEntry={"status": "CURRENT", "progress": 0})
    client.by_id[stage1["id"]] = stage1_only
    info_s1 = refresh(client, AnimeInfo(anime_id=stage1["id"], mal_id=61469, episode=1))
    assert (info_s1.mal_offset, info_s1.has_mal_sequel) == (0, True)

    mal_path = tmp_path / "mal_auth.json"
    shiki_path = tmp_path / "shiki_auth.json"
    _write_auth(mal_path, {"client_id": "c", "access_token": "mtok", "refresh_token": "mref"})
    _write_auth(
        shiki_path,
        {
            "client_id": "c",
            "client_secret": "s",
            "access_token": "stok",
            "refresh_token": "sref",
            "user_id": 655524,
        },
    )

    requests_made = []

    def mock_urlopen(request, **_kwargs):
        body = request.data.decode("utf-8") if request.data else ""
        requests_made.append((request.get_method(), request.full_url, body))
        if "api.myanimelist.net" in request.full_url:
            return _HttpResponse(b'{"num_episodes_watched": 3}')
        if request.get_method() == "GET" and "v2/user_rates" in request.full_url:
            return _HttpResponse(json.dumps([{"id": 777, "status": "watching", "rewatches": 0}]).encode())
        return _HttpResponse(json.dumps({"id": 777}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    tracker = TrackerSync(MalClient(mal_path), ShikimoriClient(shiki_path))
    service = Service(Options(), client, cache, Pending(tmp_path / "queue.json"), tracker)

    # Update Stage 2&3 Ep 2 (absolute 3): AniList receives 2, MAL & Shikimori receive 3
    outcome_s2 = service.update("/anime/SBR - 03.mkv", info_s2)
    assert outcome_s2.warnings is None
    assert client.saves[-1][:3] == (210482, Status.CURRENT, 2)

    mal_patch = [r for r in requests_made if "api.myanimelist.net" in r[1]][-1]
    mal_form = urllib.parse.parse_qs(mal_patch[2])
    assert mal_form["num_watched_episodes"] == ["3"]
    assert mal_form["status"] == ["watching"]

    shiki_patch = [r for r in requests_made if r[0] == "PATCH" and "v2/user_rates/777" in r[1]][-1]
    assert json.loads(shiki_patch[2]) == {"user_rate": {"episodes": 3, "status": "watching"}}

    # Complete Stage 1 Ep 1/1 -> AniList COMPLETED, MAL & Shikimori stay CURRENT/watching at ep 1
    requests_made.clear()
    stage1_watch = replace_info = AnimeInfo(
        anime_id=stage1["id"],
        anime_name=stage1["title"]["romaji"],
        episode=1,
        current_progress=0,
        total_episodes=1,
        current_status=Status.CURRENT,
        guessed_name="SBR",
        absolute_episode=1,
        mal_id=61469,
        mal_offset=0,
        has_mal_sequel=True,
    )
    assert replace_info.mal_id == 61469
    service.update("/anime/SBR - 01.mkv", stage1_watch)
    assert client.saves[-1][:3] == (190327, Status.COMPLETED, 1)
    mal_s1 = urllib.parse.parse_qs([r for r in requests_made if "api.myanimelist.net" in r[1]][-1][2])
    assert mal_s1["status"] == ["watching"]
    assert mal_s1["num_watched_episodes"] == ["1"]
    assert "finish_date" not in mal_s1


def test_multihop_split_cour_accumulates_all_prequels_with_same_mal_id():
    s1 = {
        "id": 101,
        "idMal": 9000,
        "title": {"romaji": "Stage 1", "english": None},
        "episodes": 4,
        "format": "TV",
        "relations": {"edges": [{"relationType": "PREQUEL", "node": {"id": 99, "idMal": 8000, "episodes": 12}}]},
    }
    s2 = {
        "id": 102,
        "idMal": 9000,
        "title": {"romaji": "Stage 2", "english": None},
        "episodes": 6,
        "format": "TV",
        "relations": {"edges": [{"relationType": "PREQUEL", "node": {"id": 101, "idMal": 9000, "episodes": 4}}]},
    }
    s3 = {
        "id": 103,
        "idMal": 9000,
        "title": {"romaji": "Stage 3", "english": None},
        "episodes": 10,
        "format": "TV",
        "relations": {"edges": [{"relationType": "PREQUEL", "node": {"id": 102, "idMal": 9000, "episodes": 6}}]},
    }
    client = _RecordingAniList(everything=[s3], by_id={101: s1, 102: s2, 103: s3})
    info = resolve(client, FileInfo("Stage 3", 2))
    assert (info.mal_offset, info.has_mal_sequel) == (10, False)


def test_correct_score_and_offline_queue_drain_sync_trackers(tmp_path, monkeypatch):
    _, stage2 = _sbr_media()
    stage2_fresh = dict(stage2, mediaListEntry={"status": "CURRENT", "progress": 1})
    client = _RecordingAniList(by_id={stage2["id"]: stage2_fresh}, score_fmt="POINT_100")

    mal_path = tmp_path / "mal_auth.json"
    shiki_path = tmp_path / "shiki_auth.json"
    _write_auth(mal_path, {"client_id": "c", "access_token": "mtok", "refresh_token": "mref"})
    _write_auth(
        shiki_path,
        {"client_id": "c", "client_secret": "s", "access_token": "stok", "refresh_token": "sref", "user_id": 1},
    )

    posts = []

    def mock_urlopen(request, **_kwargs):
        body = request.data.decode("utf-8") if request.data else ""
        posts.append((request.get_method(), request.full_url, body))
        if "api.myanimelist.net" in request.full_url:
            return _HttpResponse(b'{"num_episodes_watched": 3}')
        if request.get_method() == "GET":
            return _HttpResponse(b"[]")
        return _HttpResponse(b'{"id": 99}')

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    tracker = TrackerSync(MalClient(mal_path), ShikimoriClient(shiki_path))
    service = Service(
        Options(), client, Cache(Options(), tmp_path / "cache.json"), Pending(tmp_path / "queue.json"), tracker
    )

    # Queue while AniList is down
    info = refresh(client, AnimeInfo(anime_id=210482, mal_id=61469, episode=2, absolute_episode=3))
    client.down = True
    queued_outcome = service.update("/anime/SBR - 03.mkv", info)
    assert "will be sent later" in queued_outcome.message
    assert posts == []

    # When AniList returns, correction drains queued update AND syncs score 85 -> 8/9 on MAL & Shiki
    client.down = False
    outcome = service.correct("/anime/SBR - 03.mkv", info, Correction(210482, score=86))
    assert "Score: 86" in outcome.message
    assert any("score=9" in b for _, url, b in posts if "api.myanimelist.net" in url)
    assert any(
        json.loads(b)["user_rate"].get("episodes") == 3 for m, u, b in posts if m == "POST" and "user_rates" in u
    )
    assert any(
        json.loads(b)["user_rate"].get("score") == 9 for m, u, b in posts if m == "POST" and "user_rates" in u
    )


def test_cli_handle_emits_warnings_in_json(monkeypatch, tmp_path):
    mal_path = tmp_path / "mal_auth.json"
    _write_auth(mal_path, {"client_id": "c", "access_token": "t", "refresh_token": "r", "auth_error": True})
    monkeypatch.setattr(AniListClient, "from_token_file", lambda: FakeClient(user=[_sbr_media()[1]]))
    monkeypatch.setattr(cli, "Cache", lambda opts: Cache(opts, tmp_path / "cache.json"))
    monkeypatch.setattr(cli, "Pending", lambda: Pending(tmp_path / "queue.json"))
    monkeypatch.setattr(
        cli, "TrackerSync", lambda: TrackerSync(MalClient(mal_path), ShikimoriClient(tmp_path / "shiki.json"))
    )

    resp = cli.handle({"action": "info", "path": "/anime/JoJo no Kimyou na Bouken - Steel Ball Run - 03.mkv"})
    assert resp["ok"] is True
    assert resp["warnings"] == ["MyAnimeList: Authorization expired. Run setup_auth.py"]
