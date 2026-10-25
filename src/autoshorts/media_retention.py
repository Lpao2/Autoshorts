"""Conservative local-media retention after confirmed YouTube publication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autoshorts.state_store import PublicationStatus, StateStore


@dataclass(frozen=True, slots=True)
class MediaCleanupResult:
    publication_id: int
    removed: tuple[Path, ...] = ()
    errors: tuple[str, ...] = ()


class PublishedMediaRetention:
    """Remove local video files only after a confirmed remote publication."""

    def __init__(self, state: StateStore, root: Path) -> None:
        self.state = state
        self.root = root.expanduser().resolve(strict=True)

    def cleanup_all(self) -> tuple[MediaCleanupResult, ...]:
        return tuple(
            result
            for publication in self.state.publications()
            if publication.status is PublicationStatus.PUBLISHED
            for result in (self.cleanup(publication.publication_id),)
            if result.removed or result.errors
        )

    def cleanup(self, publication_id: int) -> MediaCleanupResult:
        publication = self.state.publication(publication_id)
        if (
            publication is None
            or publication.status is not PublicationStatus.PUBLISHED
            or not publication.remote_video_id
        ):
            return MediaCleanupResult(publication_id)

        row = self.state.connection.execute(
            """SELECT short.youtube_id, short.file_path, short.queue_path,
                      video.local_path, ready.file_path AS ready_path
               FROM publications publication
               LEFT JOIN shorts short ON short.id=publication.source_short_id
               LEFT JOIN videos video ON video.youtube_id=short.youtube_id
               LEFT JOIN ready_artifacts ready ON ready.youtube_id=short.youtube_id
               WHERE publication.id=?""",
            (publication_id,),
        ).fetchone()
        candidates = [publication.video_path]
        youtube_id = row["youtube_id"] if row else None
        if row:
            candidates.extend(
                Path(value)
                for value in (row["file_path"], row["queue_path"], row["ready_path"])
                if value
            )
            if row["local_path"] and self._source_is_fully_published(youtube_id):
                candidates.append(Path(row["local_path"]))

        removed: list[Path] = []
        errors: list[str] = []
        seen: set[Path] = set()
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve(strict=False)
                resolved.relative_to(self.root)
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(
                    f"Refusing cleanup outside retention root: {candidate}: {exc}"
                )
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if not resolved.is_file():
                continue
            if self._needed_by_unpublished_publication(resolved):
                continue
            try:
                resolved.unlink()
                removed.append(resolved)
            except OSError as exc:
                errors.append(f"Could not remove {resolved}: {exc}")
        return MediaCleanupResult(publication_id, tuple(removed), tuple(errors))

    def _source_is_fully_published(self, youtube_id: str | None) -> bool:
        if not youtube_id:
            return False
        row = self.state.connection.execute(
            """SELECT COUNT(*) AS pending
               FROM publications publication
               JOIN shorts short ON short.id=publication.source_short_id
               WHERE short.youtube_id=? AND publication.status<>?""",
            (youtube_id, PublicationStatus.PUBLISHED),
        ).fetchone()
        return bool(row and row["pending"] == 0)

    def _needed_by_unpublished_publication(self, path: Path) -> bool:
        return any(
            publication.status is not PublicationStatus.PUBLISHED
            and publication.video_path.expanduser().resolve(strict=False) == path
            for publication in self.state.publications()
        )
