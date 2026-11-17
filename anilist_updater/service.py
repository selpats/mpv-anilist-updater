"""The three actions mpv can ask for: detect the anime, record an episode, correct a detection."""

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from .api import AniListClient
from .cache import Cache, Pending
from .errors import AniListError, SkipUpdate
from .models import AnimeInfo, Correction, Options, Outcome, Status
from .parsing import parse_path
from .resolve import refresh, resolve
from .rules import Save, plan_update

if TYPE_CHECKING:
    from .trackers import TrackerSync

log = logging.getLogger(__name__)

# A missing or expired token is the user's to fix; the queue waits for it.
AUTH_ERRORS = {401}


def _fuzzy_today() -> dict[str, int]:
    today = datetime.now().astimezone().date()
    return {"year": today.year, "month": today.month, "day": today.day}


def _transition_dates(
    target_status: Status | None, prev_status: Status | None
) -> dict[str, dict[str, int]] | None:
    """Return AniList fuzzy dates when entering CURRENT or COMPLETED cleanly."""
    dates: dict[str, dict[str, int]] = {}
    if target_status in {Status.CURRENT, Status.COMPLETED} and prev_status not in {
        Status.CURRENT,
        Status.REPEATING,
        Status.COMPLETED,
    }:
        dates["startedAt"] = _fuzzy_today()
    if target_status == Status.COMPLETED and prev_status not in {Status.REPEATING, Status.COMPLETED}:
        dates["completedAt"] = _fuzzy_today()
    return dates or None


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


@dataclass(frozen=True, slots=True)
class Drained:
    """What a drain did: updates sent, updates given up on, and whether AniList asked it to wait."""

    sent: int = 0
    dropped: int = 0
    waiting: bool = False

    def __add__(self, other: "Drained") -> "Drained":
        """Both drains' counts; it is waiting if the later one is."""
        return Drained(self.sent + other.sent, self.dropped + other.dropped, other.waiting)

    def suffix(self) -> str:
        """The note for an action's message, empty when nothing happened."""
        parts = [f"{self.sent} queued sent"] * bool(self.sent) + [f"{self.dropped} dropped"] * bool(self.dropped)
        return f" (+{', '.join(parts)})" if parts else ""


def _update_message(before: AnimeInfo, after: AnimeInfo, saves: list[Save]) -> str:
    name = before.anime_name or before.guessed_name
    if before.current_progress is None and before.current_status is None:
        return f'Added "{name}" to your list with progress: {after.current_progress}'
    if len(saves) > 1 or after.current_status != before.current_status:
        return f'Updated "{name}" to {after.current_status} with progress: {after.current_progress}'
    return f'Updated "{name}" to: {after.current_progress}'


class Service:
    """Actions over an AniList client, a cache and the user's options."""

    def __init__(
        self,
        options: Options,
        client: AniListClient,
        cache: Cache,
        pending: Pending,
        tracker: "TrackerSync | None" = None,
    ) -> None:
        """Bind the collaborators."""
        self.options = options
        self.client = client
        self.cache = cache
        self.pending = pending
        self.tracker = tracker

    def _apply(
        self, path: str, info: AnimeInfo, seen: dict[int, AnimeInfo], warnings: list[str]
    ) -> tuple[AnimeInfo, list[Save], AnimeInfo]:
        """Save the info's episode; `seen` holds the entries already read this run, to not read them again.

        Returns:
            tuple[AnimeInfo, list[Save], AnimeInfo]: The entry before, the saves made and the entry after.
        """
        if known := seen.get(info.anime_id):
            info = replace(
                info,
                anime_name=known.anime_name,
                current_progress=known.current_progress,
                current_status=known.current_status,
                total_episodes=known.total_episodes,
                mal_id=known.mal_id,
                mal_offset=known.mal_offset,
                has_mal_sequel=known.has_mal_sequel,
            )
        else:
            info = refresh(self.client, info)

        prev_status = info.current_status
        saves = plan_update(info, self.options)
        for save in saves:
            if dates := _transition_dates(save.status, prev_status):
                saved = self.client.save_entry(info.anime_id, save.status, save.progress, dates=dates)
            else:
                saved = self.client.save_entry(info.anime_id, save.status, save.progress)

        updated = replace(info, current_progress=saved["progress"], current_status=Status(saved["status"]))
        seen[info.anime_id] = updated
        self.cache.put(path, updated)

        if self.tracker:
            try:
                warnings.extend(
                    self.tracker.sync_update(
                        updated, updated.current_status, prev_status, updated.current_progress
                    )
                )
            except Exception as e:
                log.warning("Secondary tracker sync failed: %s", e)

        return info, saves, updated

    def _mapped(self, path: str, info: AnimeInfo) -> AnimeInfo:
        """A queued detection as the user's correction since then maps it, if they made one."""
        entry = self.cache.get(path, info.guessed_name)
        return (entry and entry.corrected and entry.info_for(info.absolute_episode)) or info

    def _send(self, path: str, queued: AnimeInfo, seen: dict[int, AnimeInfo], warnings: list[str]) -> str:
        """Try one queued update: "sent", "dropped" (can't be done, ever), "waiting" or "failed" (kept)."""
        try:
            info, _, updated = self._apply(path, self._mapped(path, queued), seen, warnings)
        except SkipUpdate as e:
            log.info("Dropped queued update (%s): %s", queued.anime_name, e)
            return "dropped"
        except AniListError as e:
            if e.retryable or e.status in AUTH_ERRORS:
                log.info("Queued updates wait, AniList says: %s", e)
                return "waiting"
            log.warning("Dropped queued update (%s): %s", queued.anime_name, e)
            return "dropped"
        except Exception:
            log.warning("Couldn't send queued update (%s); keeping it", queued.anime_name, exc_info=True)
            return "failed"
        log.info('Sent queued update: "%s" to %s', info.anime_name, updated.current_progress)
        return "sent"

    def drain(
        self,
        seen: dict[int, AnimeInfo] | None = None,
        anime_id: int | None = None,
        warnings: list[str] | None = None,
    ) -> Drained:
        """Send the queued updates, oldest first. Only called once a request has just worked.

        Each one leaves the queue only after it is saved, so a run killed in between sends it again; the
        second time the episode is no longer new and is dropped. Failures never escape: the action it runs in
        goes on, an update that failed unexpectedly stays queued, and AniList failing again stops the rest.

        Args:
            seen (dict[int, AnimeInfo] | None): Entries already read this run, to not read them again.
            anime_id (int | None): Send only the updates of this anime.
            warnings (list[str] | None): Optional collector for secondary tracker warnings.

        Returns:
            Drained: How many updates were sent and dropped, and whether AniList made it stop.
        """
        sent = dropped = 0
        seen = {} if seen is None else seen
        warnings = [] if warnings is None else warnings
        waiting = False
        for path, queued in self.pending.items():
            if anime_id is not None and queued.anime_id != anime_id:
                continue
            result = self._send(path, queued, seen, warnings)
            sent += result == "sent"
            dropped += result == "dropped"
            if result == "waiting":
                waiting = True
                break
            if result != "failed":
                self.pending.remove(path, queued)
        self.cache.save()
        return Drained(sent, dropped, waiting)

    def _queue(self, path: str, info: AnimeInfo, reason: object, warnings: list[str]) -> Outcome:
        self.pending.add(path, info)
        name = info.anime_name or info.guessed_name
        log.info("Queued update: %s", reason)
        return Outcome(
            info,
            f'AniList is unreachable, "{name}" episode {info.episode} will be sent later.',
            warnings=_dedupe(warnings) or None,
        )

    def info(self, path: str) -> Outcome:
        """Detect the anime and episode for a file, from the cache when it knows the directory.

        Raises:
            AniListError: If the anime has to be looked up and AniList can't answer.
        """
        warnings: list[str] = self.tracker.check_auth() if self.tracker else []

        file = parse_path(path)
        entry = self.cache.get(path, file.query)
        cached = entry.info_for(file.episode) if entry else None
        if cached:
            log.info('Using cached data for "%s": %s', file.query, cached.anime_id)

        info = cached or resolve(self.client, file)

        # An unfinished season has no episode count at first; corrected and sliding entries fill it in later.
        stale = cached and (entry.corrected or self.cache.sliding) and info.total_episodes is None
        if stale:
            try:
                info = refresh(self.client, info)
            except AniListError as e:
                if not e.retryable:
                    raise
                log.info("Keeping the cached data, couldn't refresh it: %s", e)
                stale = False

        # A cache hit isn't stored again, or its TTL would never run out.
        if (not cached or stale) and info.current_progress is not None:
            self.cache.put(path, info)
        self.cache.save()

        # Only a detection that asked AniList shows it is reachable.
        if not cached or stale:
            self.drain(warnings=warnings)
        return Outcome(info, warnings=_dedupe(warnings) or None)

    def update(self, path: str, info: AnimeInfo) -> Outcome:
        """Record the info's episode on AniList according to the user's options.

        If AniList can't be reached the update is queued instead. Queued episodes of the same anime go first,
        so they don't land behind this one; the other queued ones are sent once it has worked, or been skipped
        after AniList answered.

        Raises:
            AniListError: If AniList refuses the update, for a reason waiting won't fix.
            SkipUpdate: If the episode shouldn't be recorded.
        """
        seen: dict[int, AnimeInfo] = {}
        warnings: list[str] = []
        first = Drained()
        try:
            if info.anime_id is not None:
                first = self.drain(seen, info.anime_id, warnings)
            if first.waiting:
                return self._queue(path, info, "AniList is still unreachable", warnings)
            before, saves, updated = self._apply(path, info, seen, warnings)
        except AniListError as e:
            if not e.retryable:
                raise
            return self._queue(path, info, e, warnings)
        except SkipUpdate:
            self.drain(seen, warnings=warnings)
            raise
        finally:
            self.cache.save()
        drained = first + self.drain(seen, warnings=warnings)
        return Outcome(
            updated,
            _update_message(before, updated, saves) + drained.suffix(),
            warnings=_dedupe(warnings) or None,
        )

    def correct(self, path: str, info: AnimeInfo | None, correction: Correction) -> Outcome:
        """Apply the user's fixes to a detection: the AniList entry, the episode mapping and the status.

        Raises:
            SkipUpdate: If the correction changes nothing.
        """
        if info is None:
            file = parse_path(path, skip=False)
            info = AnimeInfo(episode=file.episode, guessed_name=file.query, absolute_episode=file.episode)

        changes = []
        id_changed = info.anime_id != correction.anime_id
        if id_changed:
            changes.append(f"ID: {info.anime_id or '?'}->{correction.anime_id}")
            info = refresh(self.client, info, correction.anime_id)

        current_episode = info.episode or info.absolute_episode
        episode = max(1, correction.episode or current_episode)
        if correction.episode and info.total_episodes and episode > info.total_episodes:
            raise SkipUpdate(f"Episode {episode} is past the last of {info.total_episodes}.")
        episode_changed = episode != current_episode
        if episode_changed:
            changes.append(f"Mapped {info.absolute_episode}->{episode}")

        prev_status = info.current_status
        status = correction.status if correction.status != info.current_status else None
        score = correction.score
        warnings: list[str] = []
        if status or score is not None:
            if status:
                changes.append(f"Status: {info.current_status or 'None'}->{status}")
            if score is not None:
                changes.append(f"Score: {score:g}" if score else "Score: cleared")
            if dates := _transition_dates(status, prev_status):
                self.client.save_entry(correction.anime_id, status, score=score, dates=dates)
            else:
                self.client.save_entry(correction.anime_id, status, score=score)
            info = replace(info, current_status=status or info.current_status)

            if self.tracker and info.mal_id:
                fmt = correction.score_format
                if score is not None and fmt is None:
                    try:
                        fmt = self.client.score_format()
                    except Exception as e:
                        log.debug("Couldn't fetch AniList score format for secondary sync: %s", e)
                try:
                    warnings.extend(
                        self.tracker.sync_correction(info, status, prev_status, score=score, score_format=fmt)
                    )
                except Exception as e:
                    log.warning("Secondary tracker sync failed on correction: %s", e)

        if not changes:
            raise SkipUpdate("No correction changes detected.")

        info = replace(info, episode=episode)
        self.cache.put(path, info, corrected=id_changed or episode_changed)
        self.cache.save()
        self.drain(warnings=warnings)
        return Outcome(
            info,
            f'Corrected "{info.anime_name}" (ID: {info.anime_id}) | ' + " | ".join(changes),
            warnings=_dedupe(warnings) or None,
        )
