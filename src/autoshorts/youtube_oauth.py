"""Explicit OAuth connection and verification for YouTube publishing channels."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from autoshorts.automation_config import AutomationConfig, PublishingChannel
from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.state_store import ChannelCredentialBinding, StateStore

CLIENT_SECRET_ENV = "AUTOSHORTS_GOOGLE_CLIENT_SECRET"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube"
# One consent grants all capabilities used by onboarding: channel inspection,
# video upload, channel metadata updates, and banner provisioning.
YOUTUBE_SCOPES = (YOUTUBE_MANAGE_SCOPE,)


@dataclass(frozen=True, slots=True)
class YouTubeChannelIdentity:
    channel_id: str
    title: str


@dataclass(frozen=True, slots=True)
class PendingChannelConnection:
    publishing_channel_id: str
    google_identity_id: str
    channel: YouTubeChannelIdentity
    credential_payload: str = field(repr=False)


class OAuthCredentialError(RuntimeError):
    """Safe user-facing OAuth failure that never includes credential payloads."""


class OAuthBackend(Protocol):
    def authorize(
        self, client_secret_path: Path, scopes: tuple[str, ...]
    ) -> object: ...
    def load(self, payload: str, scopes: tuple[str, ...]) -> object: ...
    def serialize(self, credentials: object) -> str: ...
    def identify_channel(self, credentials: object) -> YouTubeChannelIdentity: ...


class GoogleYouTubeOAuthBackend:
    """Lazy imports keep non-OAuth installations usable."""

    def authorize(self, client_secret_path: Path, scopes: tuple[str, ...]) -> object:
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise RuntimeError(
                "Instale o extra 'youtube-oauth' para conectar ao YouTube."
            ) from exc
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret_path), scopes=list(scopes)
        )
        return flow.run_local_server(
            host="127.0.0.1",
            port=0,
            access_type="offline",
            prompt="consent",
            open_browser=True,
        )

    def load(self, payload: str, scopes: tuple[str, ...]) -> object:
        from google.oauth2.credentials import Credentials

        return Credentials.from_authorized_user_info(json.loads(payload), list(scopes))

    def serialize(self, credentials: object) -> str:
        return str(credentials.to_json())  # type: ignore[attr-defined]

    def identify_channel(self, credentials: object) -> YouTubeChannelIdentity:
        try:
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Instale o extra 'youtube-oauth' para verificar o canal."
            ) from exc
        if getattr(credentials, "expired", False):
            if not getattr(credentials, "refresh_token", None):
                raise RuntimeError("Credencial expirada sem refresh token.")
            credentials.refresh(Request())  # type: ignore[attr-defined]
        response = (
            build("youtube", "v3", credentials=credentials, cache_discovery=False)
            .channels()
            .list(part="snippet", mine=True)
            .execute()
        )
        items = response.get("items", [])
        if len(items) != 1:
            raise RuntimeError(
                "A credencial não retornou exatamente um canal YouTube autenticado."
            )
        item = items[0]
        return YouTubeChannelIdentity(str(item["id"]), str(item["snippet"]["title"]))


class YouTubeConnectionService:
    def __init__(
        self,
        config: AutomationConfig,
        state_store: StateStore,
        credential_store: FilesystemCredentialStore,
        backend: OAuthBackend | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.credential_store = credential_store
        self.backend = backend or GoogleYouTubeOAuthBackend()

    def begin_connection(self, publishing_channel_id: str) -> PendingChannelConnection:
        channel = self._channel(publishing_channel_id)
        if not channel.enabled:
            raise ValueError("O canal de publicação está desativado.")
        if not channel.google_identity_id:
            raise ValueError("O canal precisa de uma Google Identity antes do OAuth.")
        identity = next(
            item
            for item in self.config.google_identities
            if item.identity_id == channel.google_identity_id
        )
        if not identity.enabled:
            raise ValueError("A Google Identity vinculada está desativada.")
        client_secret = self._client_secret_path()
        credentials = self.backend.authorize(client_secret, YOUTUBE_SCOPES)
        identity = self.backend.identify_channel(credentials)
        return PendingChannelConnection(
            channel.channel_id,
            channel.google_identity_id,
            identity,
            self.backend.serialize(credentials),
        )

    def confirm_connection(
        self, pending: PendingChannelConnection
    ) -> ChannelCredentialBinding:
        channel = self._channel(pending.publishing_channel_id)
        if channel.google_identity_id != pending.google_identity_id:
            raise ValueError("A Google Identity do canal mudou durante a conexão.")
        conflict = self.state_store.get_channel_binding_by_youtube_id(
            pending.channel.channel_id
        )
        if conflict and conflict.publishing_channel_id != channel.channel_id:
            raise ValueError(
                "Este YouTube Channel ID já está vinculado a outro canal de "
                "publicação."
            )
        previous = self.state_store.get_channel_binding(channel.channel_id)
        previous_payload = (
            self.credential_store.read(previous.credential_ref) if previous else None
        )
        credential_ref = self.credential_store.write(
            channel.channel_id, pending.credential_payload
        )
        now = datetime.now(UTC).isoformat()
        binding = ChannelCredentialBinding(
            channel.channel_id,
            pending.google_identity_id,
            "youtube",
            credential_ref,
            pending.channel.channel_id,
            pending.channel.title,
            now,
            now,
            "CONNECTED",
        )
        try:
            self.state_store.save_channel_binding(binding)
        except Exception:
            if previous_payload is None:
                self.credential_store.delete(credential_ref)
            else:
                self.credential_store.write(channel.channel_id, previous_payload)
            raise
        return binding

    def confirm_expected_connection(
        self,
        pending: PendingChannelConnection,
        expected_youtube_channel_id: str,
    ) -> ChannelCredentialBinding:
        """Persist a guided OAuth result only when it is the planned channel."""
        if pending.channel.channel_id != expected_youtube_channel_id:
            raise OAuthCredentialError(
                "OAUTH_CHANNEL_MISMATCH: o canal autorizado não corresponde ao "
                "Channel ID planejado. Nenhuma credencial foi persistida."
            )
        return self.confirm_connection(pending)

    def verify_binding(self, publishing_channel_id: str) -> ChannelCredentialBinding:
        binding = self.state_store.get_channel_binding(publishing_channel_id)
        if binding is None:
            raise KeyError(f"Canal sem binding: {publishing_channel_id}")
        channel = self._channel(publishing_channel_id)
        if channel.google_identity_id != binding.google_identity_id:
            self._mark_invalid(binding, "INVALID")
            raise OAuthCredentialError(
                "O binding OAuth pertence a outra Google Identity. Reconecte o canal."
            )
        try:
            payload = self.credential_store.read(binding.credential_ref)
            self._require_current_scopes(payload)
            credentials = self.backend.load(payload, YOUTUBE_SCOPES)
            actual = self.backend.identify_channel(credentials)
        except OAuthCredentialError:
            raise
        except Exception:
            self._mark_invalid(binding, "REVOKED")
            raise OAuthCredentialError("OAuth credential revoked or invalid.") from None
        if actual.channel_id != binding.youtube_channel_id:
            self._mark_invalid(binding, "INVALID")
            raise ValueError(
                "O canal autenticado não corresponde ao youtube_channel_id persistido."
            )
        refreshed_payload = self.backend.serialize(credentials)
        self.credential_store.write(publishing_channel_id, refreshed_payload)
        verified = ChannelCredentialBinding(
            binding.publishing_channel_id,
            binding.google_identity_id,
            binding.provider,
            binding.credential_ref,
            binding.youtube_channel_id,
            actual.title,
            binding.connected_at,
            datetime.now(UTC).isoformat(),
            "CONNECTED",
        )
        self.state_store.save_channel_binding(verified)
        return verified

    def load_verified_credentials(self, publishing_channel_id: str) -> object:
        """Verify the remote channel, persist refreshes, then reload credentials."""
        binding = self.verify_binding(publishing_channel_id)
        try:
            payload = self.credential_store.read(binding.credential_ref)
            return self.backend.load(payload, YOUTUBE_SCOPES)
        except Exception:
            self._mark_invalid(binding, "REVOKED")
            raise OAuthCredentialError("OAuth credential revoked or invalid.") from None

    def disconnect(self, publishing_channel_id: str) -> bool:
        binding = self.state_store.get_channel_binding(publishing_channel_id)
        if binding is None:
            return False
        self.credential_store.delete(binding.credential_ref)
        self.state_store.delete_channel_binding(publishing_channel_id)
        return True

    def _channel(self, publishing_channel_id: str) -> PublishingChannel:
        for theme in self.config.themes:
            for channel in theme.channels:
                if channel.channel_id == publishing_channel_id:
                    return channel
        raise KeyError(f"Publishing channel not found: {publishing_channel_id}")

    def _mark_invalid(
        self, binding: ChannelCredentialBinding, status: str
    ) -> None:
        self.state_store.save_channel_binding(replace(binding, status=status))

    @staticmethod
    def _require_current_scopes(payload: str) -> None:
        scopes = json.loads(payload).get("scopes")
        if scopes is not None and not set(YOUTUBE_SCOPES).issubset(scopes):
            raise OAuthCredentialError(
                "OAuth scope outdated; reconnect the channel to authorize branding."
            )

    @staticmethod
    def _client_secret_path() -> Path:
        configured = os.environ.get(CLIENT_SECRET_ENV)
        if not configured:
            raise ValueError(f"Defina {CLIENT_SECRET_ENV} fora do repositório.")
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise ValueError(
                f"O arquivo configurado em {CLIENT_SECRET_ENV} não existe."
            )
        return path
