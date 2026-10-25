from __future__ import annotations

from test_youtube_publishing import configured_service

from autoshorts.media_retention import PublishedMediaRetention
from autoshorts.state_store import PublicationStatus
from autoshorts.youtube_publishing import PublicationMetadata


def _publication(tmp_path):
    service, store, channel, _plan, video, _uploader, _connection = (
        configured_service(tmp_path)
    )
    publication = service.create_for_review(
        channel.channel_id, video, PublicationMetadata("Retention test")
    )
    service.approve(publication.publication_id)
    return service, store, video, publication.publication_id


def test_confirmed_publication_media_is_removed(tmp_path):
    service, store, video, publication_id = _publication(tmp_path)
    service.upload_approved(publication_id)

    result = PublishedMediaRetention(store, tmp_path).cleanup(publication_id)

    assert result.removed == (video.resolve(),)
    assert result.errors == ()
    assert not video.exists()
    assert store.publication(publication_id).remote_video_id == "remote-123"
    assert len(store.publication_attempts(publication_id)) == 1
    store.close()


def test_approved_media_is_preserved(tmp_path):
    _service, store, video, publication_id = _publication(tmp_path)

    result = PublishedMediaRetention(store, tmp_path).cleanup(publication_id)

    assert result.removed == ()
    assert video.exists()
    store.close()


def test_uncertain_media_is_preserved(tmp_path):
    _service, store, video, publication_id = _publication(tmp_path)
    store.connection.execute(
        "UPDATE publications SET status=? WHERE id=?",
        (PublicationStatus.UPLOAD_UNCERTAIN, publication_id),
    )

    result = PublishedMediaRetention(store, tmp_path).cleanup(publication_id)

    assert result.removed == ()
    assert video.exists()
    store.close()


def test_cleanup_refuses_paths_outside_retention_root(tmp_path):
    outside = tmp_path / "outside"
    root = tmp_path / "retention"
    outside.mkdir()
    root.mkdir()
    service, store, video, publication_id = _publication(outside)
    service.upload_approved(publication_id)

    result = PublishedMediaRetention(store, root).cleanup(publication_id)

    assert result.removed == ()
    assert result.errors
    assert video.exists()
    store.close()


def test_cleanup_all_is_idempotent(tmp_path):
    service, store, video, publication_id = _publication(tmp_path)
    service.upload_approved(publication_id)
    retention = PublishedMediaRetention(store, tmp_path)

    assert retention.cleanup_all()[0].removed == (video.resolve(),)
    assert retention.cleanup_all() == ()
    store.close()
