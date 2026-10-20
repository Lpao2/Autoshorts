"""Reviewed, idempotent publication of one local video through YouTube Data API."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from autoshorts.automation_config import (
    AutomationConfig,
    GoogleIdentity,
    PublishingChannel,
)
from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.media_probe import MediaInfo, probe_media
from autoshorts.state_store import (
    PublicationRecord,
    PublicationStatus,
    StateStore,
)
from autoshorts.youtube_oauth import YouTubeConnectionService

PRIVACY_STATUSES = frozenset({"private", "unlisted", "public"})


@dataclass(frozen=True, slots=True)
class PublicationMetadata:
    title: str
    description: str = ""
    tags: tuple[str, ...] = ()
    privacy_status: str = "private"
    made_for_kids: bool | None = False
    contains_synthetic_media: bool | None = False
    has_paid_product_placement: bool | None = False
    category_id: str | None = None
    category_name: str | None = None
    default_language: str = "pt-BR"
    license: str = "youtube"
    embeddable: bool = True
    public_stats_viewable: bool = True
    publish_at: str | None = None
    recording_date: str | None = None

    def validated(self) -> PublicationMetadata:
        title = self.title.strip()
        description = self.description.strip()
        tags = tuple(dict.fromkeys(item.strip() for item in self.tags if item.strip()))
        if not title:
            raise ValueError("Publication title cannot be empty.")
        if len(title) > 100:
            raise ValueError("YouTube title cannot exceed 100 characters.")
        if len(description) > 5000:
            raise ValueError("YouTube description cannot exceed 5000 characters.")
        if self.privacy_status not in PRIVACY_STATUSES:
            raise ValueError("Privacy status must be private, unlisted or public.")
        if any(len(item) > 500 for item in tags):
            raise ValueError("A publication tag is too long.")
        if len(",".join(tags)) > 500:
            raise ValueError("Combined YouTube tags cannot exceed 500 characters.")
        if self.made_for_kids is None:
            raise ValueError("Made for kids must be explicitly declared.")
        if self.contains_synthetic_media is None:
            raise ValueError("Synthetic/altered media must be explicitly declared.")
        if self.has_paid_product_placement is None:
            raise ValueError("Paid product placement must be explicitly declared.")
        if not self.category_id or not self.category_id.isdigit():
            raise ValueError("A numeric YouTube category ID is required.")
        if not self.default_language.strip():
            raise ValueError("Default language is required.")
        if self.license not in {"youtube", "creativeCommon"}:
            raise ValueError("License must be youtube or creativeCommon.")
        for label, value in (
            ("publish_at", self.publish_at),
            ("recording_date", self.recording_date),
        ):
            if value:
                try:
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError(f"{label} must use ISO 8601 format.") from exc
        if self.publish_at and self.privacy_status != "private":
            raise ValueError("Scheduled publishing requires private privacy status.")
        return PublicationMetadata(
            title,
            description,
            tags,
            self.privacy_status,
            self.made_for_kids,
            self.contains_synthetic_media,
            self.has_paid_product_placement,
            self.category_id,
            self.category_name,
            self.default_language.strip(),
            self.license,
            self.embeddable,
            self.public_stats_viewable,
            self.publish_at,
            self.recording_date,
        )


@dataclass(frozen=True, slots=True)
class PublicationPreflight:
    publication: PublicationRecord
    channel_name: str
    google_identity_label: str
    youtube_channel_title: str
    media: MediaInfo
    oauth_verified: bool


class YouTubeUploadBackend(Protocol):
    def upload(
        self,
        credentials: object,
        video_path: Path,
        metadata: PublicationMetadata,
    ) -> str: ...


class YouTubeUploadFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        outcome_uncertain: bool,
        category: str,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.outcome_uncertain = outcome_uncertain
        self.category = category


class GoogleYouTubeUploadBackend:
    """Official resumable videos.insert adapter with bounded transport retries."""

    def upload(
        self,
        credentials: object,
        video_path: Path,
        metadata: PublicationMetadata,
    ) -> str:
        try:
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise RuntimeError(
                "Instale o extra 'youtube-oauth' para publicar no YouTube."
            ) from exc
        media = MediaFileUpload(
            str(video_path), chunksize=8 * 1024 * 1024, resumable=True
        )
        body = {
            "snippet": {
                "title": metadata.title,
                "description": metadata.description,
                "tags": list(metadata.tags),
                "categoryId": metadata.category_id,
                "defaultLanguage": metadata.default_language,
            },
            "status": {
                "privacyStatus": metadata.privacy_status,
                "selfDeclaredMadeForKids": metadata.made_for_kids,
                "containsSyntheticMedia": metadata.contains_synthetic_media,
                "license": metadata.license,
                "embeddable": metadata.embeddable,
                "publicStatsViewable": metadata.public_stats_viewable,
            },
            "paidProductPlacementDetails": {
                "hasPaidProductPlacement": metadata.has_paid_product_placement
            },
        }
        parts = ["snippet", "status", "paidProductPlacementDetails"]
        if metadata.publish_at:
            body["status"]["publishAt"] = metadata.publish_at
        if metadata.recording_date:
            body["recordingDetails"] = {"recordingDate": metadata.recording_date}
            parts.append("recordingDetails")
        try:
            request = (
                build("youtube", "v3", credentials=credentials, cache_discovery=False)
                .videos()
                .insert(part=",".join(parts), body=body, media_body=media)
            )
        except Exception:
            raise YouTubeUploadFailure(
                "Não foi possível iniciar o upload oficial do YouTube.",
                retryable=False,
                outcome_uncertain=False,
                category="CLIENT_OR_REQUEST",
            ) from None
        response = None
        try:
            while response is None:
                _status, response = request.next_chunk(num_retries=3)
        except Exception:
            raise YouTubeUploadFailure(
                "O upload foi interrompido e o resultado remoto é incerto. "
                "Verifique o YouTube Studio antes de tentar novamente.",
                retryable=False,
                outcome_uncertain=True,
                category="REMOTE_OUTCOME_UNCERTAIN",
            ) from None
        remote_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(remote_id, str) or not remote_id.strip():
            raise YouTubeUploadFailure(
                "O YouTube respondeu sem um video ID; verifique o YouTube Studio.",
                retryable=False,
                outcome_uncertain=True,
                category="INCONSISTENT_RESPONSE",
            )
        return remote_id.strip()


class YouTubePublicationService:
    def __init__(
        self,
        config: AutomationConfig,
        state_store: StateStore,
        credential_store: FilesystemCredentialStore,
        *,
        connection_service: YouTubeConnectionService | None = None,
        upload_backend: YouTubeUploadBackend | None = None,
        media_probe: Callable[[Path], MediaInfo] = probe_media,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.credential_store = credential_store
        self.connection_service = connection_service or YouTubeConnectionService(
            config, state_store, credential_store
        )
        self.upload_backend = upload_backend or GoogleYouTubeUploadBackend()
        self.media_probe = media_probe

    def create_for_review(
        self,
        publishing_channel_id: str,
        video_path: Path,
        metadata: PublicationMetadata,
    ) -> PublicationRecord:
        channel, identity = self._target(publishing_channel_id)
        theme = next(t for t in self.config.themes if channel in t.channels)
        if metadata.category_id is None:
            market = self.state_store.theme_market_profile(
                theme.output_subdirectory,
                "youtube",
                "BR",
                metadata.default_language or "pt-BR",
            )
            if market is None or not market.enabled:
                raise ValueError(
                    "O tema não possui perfil editorial ativo para youtube/BR/"
                    f"{metadata.default_language or 'pt-BR'}."
                )
            metadata = replace(
                metadata,
                category_id=market.youtube_category_id,
                category_name=market.youtube_category_name,
            )
        binding = self._connected_binding(channel, identity)
        kit = self.state_store.get_approved_brand_kit(channel.channel_id)
        if kit is None or not kit.source_plan_id:
            raise ValueError(
                "O canal precisa de um Brand Plan operacional aprovado antes da "
                "publicação."
            )
        plan = self._brand_plan(kit.source_plan_id)
        self._validate_brand_assets(plan.brand_plan_id)
        path = Path(video_path).expanduser().resolve()
        self.media_probe(path)
        checksum = _checksum(path)
        source_short = self.state_store.short_by_path(path)
        if (
            source_short is not None
            and source_short.publishing_channel_id
            and source_short.publishing_channel_id != channel.channel_id
        ):
            raise ValueError(
                "The selected Short is queued for a different publishing channel."
            )
        clean = metadata.validated()
        key = _idempotency_key(channel.channel_id, checksum)
        existing = self.state_store.publication_by_key(key)
        if existing is not None:
            if _publication_metadata(existing) != clean:
                raise ValueError(
                    "Este vídeo já possui uma publicação para o canal. Revise o "
                    "registro existente em vez de criar outro."
                )
            return existing
        created = self.state_store.create_publication(
            idempotency_key=key,
            brand_plan_id=plan.brand_plan_id,
            publishing_channel_id=channel.channel_id,
            google_identity_id=identity.identity_id,
            youtube_channel_id=binding.youtube_channel_id,
            source_short_id=source_short.id if source_short else None,
            video_path=path,
            video_checksum=checksum,
            title=clean.title,
            description=clean.description,
            tags=clean.tags,
            privacy_status=clean.privacy_status,
            made_for_kids=clean.made_for_kids,
            contains_synthetic_media=clean.contains_synthetic_media,
            has_paid_product_placement=clean.has_paid_product_placement,
            category_id=clean.category_id,
            category_name=clean.category_name,
            default_language=clean.default_language,
            license=clean.license,
            embeddable=clean.embeddable,
            public_stats_viewable=clean.public_stats_viewable,
            publish_at=clean.publish_at,
            recording_date=clean.recording_date,
        )
        if _publication_metadata(created) != clean:
            raise ValueError(
                "A concurrent publication already exists with different metadata."
            )
        return created

    def approve(self, publication_id: int) -> PublicationRecord:
        self._local_preflight(publication_id)
        return self.state_store.approve_publication(publication_id)

    def revise(
        self, publication_id: int, metadata: PublicationMetadata
    ) -> PublicationRecord:
        publication, _channel, _identity, _media = self._local_preflight(publication_id)
        if metadata.category_id is None:
            metadata = replace(
                metadata,
                category_id=publication.category_id,
                category_name=publication.category_name,
            )
        clean = metadata.validated()
        return self.state_store.revise_publication(
            publication.publication_id,
            title=clean.title,
            description=clean.description,
            tags=clean.tags,
            privacy_status=clean.privacy_status,
            made_for_kids=clean.made_for_kids,
            contains_synthetic_media=clean.contains_synthetic_media,
            has_paid_product_placement=clean.has_paid_product_placement,
            category_id=clean.category_id,
            category_name=clean.category_name,
            default_language=clean.default_language,
            license=clean.license,
            embeddable=clean.embeddable,
            public_stats_viewable=clean.public_stats_viewable,
            publish_at=clean.publish_at,
            recording_date=clean.recording_date,
        )

    def preflight(self, publication_id: int) -> PublicationPreflight:
        publication, channel, identity, media = self._local_preflight(publication_id)
        binding = self.connection_service.verify_binding(channel.channel_id)
        if binding.youtube_channel_id != publication.youtube_channel_id:
            raise ValueError("O destino remoto mudou desde a criação da publicação.")
        return PublicationPreflight(
            publication,
            channel.name,
            identity.label,
            binding.youtube_channel_title,
            media,
            True,
        )

    def upload_approved(self, publication_id: int) -> PublicationRecord:
        current = self.state_store.publication(publication_id)
        if current is None:
            raise KeyError(f"Unknown publication: {publication_id}")
        if current.status == PublicationStatus.PUBLISHED:
            return current
        publication, channel, _identity, _media = self._local_preflight(publication_id)
        if publication.status != PublicationStatus.APPROVED_FOR_PUBLISH:
            raise ValueError("Publication must be explicitly approved before upload.")
        credentials = self.connection_service.load_verified_credentials(
            channel.channel_id
        )
        publication, attempt = self.state_store.start_publication_attempt(
            publication_id
        )
        metadata = _publication_metadata(publication)
        try:
            remote_id = self.upload_backend.upload(
                credentials, publication.video_path, metadata
            )
        except YouTubeUploadFailure as exc:
            if exc.outcome_uncertain:
                status = PublicationStatus.UPLOAD_UNCERTAIN
            elif exc.retryable:
                status = PublicationStatus.FAILED_RETRYABLE
            else:
                status = PublicationStatus.FAILED_PERMANENT
            self.state_store.finish_publication_attempt(
                publication_id,
                attempt.attempt_id,
                status=status,
                error_category=exc.category,
                error_message=str(exc),
            )
            raise
        except Exception:
            message = (
                "O upload falhou com resultado remoto incerto. Verifique o "
                "YouTube Studio antes de tentar novamente."
            )
            self.state_store.finish_publication_attempt(
                publication_id,
                attempt.attempt_id,
                status=PublicationStatus.UPLOAD_UNCERTAIN,
                error_category="UNEXPECTED_REMOTE_ERROR",
                error_message=message,
            )
            raise YouTubeUploadFailure(
                message,
                retryable=False,
                outcome_uncertain=True,
                category="UNEXPECTED_REMOTE_ERROR",
            ) from None
        return self.state_store.finish_publication_attempt(
            publication_id,
            attempt.attempt_id,
            status=PublicationStatus.PUBLISHED,
            remote_video_id=remote_id,
        )

    def _local_preflight(
        self, publication_id: int
    ) -> tuple[PublicationRecord, PublishingChannel, GoogleIdentity, MediaInfo]:
        publication = self.state_store.publication(publication_id)
        if publication is None:
            raise KeyError(f"Unknown publication: {publication_id}")
        channel, identity = self._target(publication.publishing_channel_id)
        if identity.identity_id != publication.google_identity_id:
            raise ValueError("A Google Identity mudou desde a criação da publicação.")
        binding = self._connected_binding(channel, identity)
        if binding.youtube_channel_id != publication.youtube_channel_id:
            raise ValueError("O YouTube Channel ID mudou desde a criação do pacote.")
        kit = self.state_store.get_approved_brand_kit(channel.channel_id)
        if kit is None or kit.source_plan_id != publication.brand_plan_id:
            raise ValueError("O Brand Plan operacional mudou ou não está aprovado.")
        self._brand_plan(publication.brand_plan_id)
        self._validate_brand_assets(publication.brand_plan_id)
        path = publication.video_path
        if _checksum(path) != publication.video_checksum:
            raise ValueError("O arquivo de vídeo mudou desde a criação do pacote.")
        media = self.media_probe(path)
        _publication_metadata(publication).validated()
        return publication, channel, identity, media

    def _target(
        self, publishing_channel_id: str
    ) -> tuple[PublishingChannel, GoogleIdentity]:
        channel = next(
            (
                item
                for theme in self.config.themes
                for item in theme.channels
                if item.channel_id == publishing_channel_id
            ),
            None,
        )
        if channel is None:
            raise KeyError(f"Publishing channel not found: {publishing_channel_id}")
        if not channel.enabled:
            raise ValueError("Publishing channel is disabled.")
        identity = next(
            (
                item
                for item in self.config.google_identities
                if item.identity_id == channel.google_identity_id
            ),
            None,
        )
        if identity is None or not identity.enabled:
            raise ValueError("Publishing channel has no enabled Google Identity.")
        return channel, identity

    def _connected_binding(self, channel, identity):
        binding = self.state_store.get_channel_binding(channel.channel_id)
        if binding is None or binding.status != "CONNECTED":
            raise ValueError("Publishing channel has no valid OAuth binding.")
        if binding.google_identity_id != identity.identity_id:
            raise ValueError("OAuth binding belongs to another Google Identity.")
        return binding

    def _brand_plan(self, brand_plan_id: str):
        plan = next(
            (
                item
                for item in self.state_store.planned_channel_brands()
                if item.brand_plan_id == brand_plan_id
            ),
            None,
        )
        if plan is None or plan.status.value != "APPROVED":
            raise ValueError("Linked Brand Plan is not approved.")
        return plan

    def _validate_brand_assets(self, brand_plan_id: str) -> None:
        from autoshorts.brand_assets import BrandAssetStatus, BrandAssetType

        assets = self.state_store.brand_assets(brand_plan_id)
        for asset_type in BrandAssetType:
            matching = [item for item in assets if item.asset_type == asset_type]
            current = max(matching, key=lambda item: item.version, default=None)
            if current is None or current.status != BrandAssetStatus.APPROVED:
                raise ValueError(f"Current {asset_type.value.lower()} is not approved.")


def _checksum(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Video file not found: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publication_metadata(publication: PublicationRecord) -> PublicationMetadata:
    return PublicationMetadata(
        publication.title,
        publication.description,
        publication.tags,
        publication.privacy_status,
        publication.made_for_kids,
        publication.contains_synthetic_media,
        publication.has_paid_product_placement,
        publication.category_id,
        publication.category_name,
        publication.default_language or "",
        publication.license or "",
        bool(publication.embeddable),
        bool(publication.public_stats_viewable),
        publication.publish_at,
        publication.recording_date,
    )


def _idempotency_key(publishing_channel_id: str, video_checksum: str) -> str:
    value = f"youtube:{publishing_channel_id}:{video_checksum}".encode()
    return hashlib.sha256(value).hexdigest()
