# READY metadata review flow

Generate or reuse a persisted metadata draft for a READY artifact:

```console
.venv/bin/autoshorts-produce metadata \
  --source-video-id U4ZWhmwcLj8 \
  --channel Tiltado
```

The command uses the selected segment transcript and reason together with the
source title, theme guidance, market profile, Brand Plan, and destination channel.
It does not create a Publication or scheduler job while the effective policy is
`MANUAL_REVIEW`.

After explicit human approval, the same CLI can create and approve the Publication
and enqueue it into the next slot resolved by the persisted posting profile:

```console
.venv/bin/autoshorts-produce approve-metadata 1
```

This command only persists the approved Publication and scheduled job. It does not
run the scheduler or upload media. Both draft generation and approval/enqueue are
idempotent. The default review policy is stored in `metadata_settings`; optional
per-channel overrides are stored in `metadata_channel_settings`. Changing the
effective policy to `AUTO_APPROVE` allows a future orchestrator to call the same
generation and approval functions without a human checkpoint.

## Publication privacy

Set `AUTOSHORTS_PUBLICATION_PRIVACY=public` in the orchestrator environment to
generate new drafts and Publications with public visibility. The default remains
`private`. Restart the service after changing its environment. Existing drafts,
queued Publications and remote videos retain their persisted visibility.
