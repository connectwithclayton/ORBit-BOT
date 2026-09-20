"""Live Gmail poller — stub only.

Slice 4 does not poll a mailbox. Intents come from anonymized shelf fixtures
or a local already-normalized JSONL queue. Do not put OAuth tokens, app
passwords, refresh tokens, or captain addresses in this repo.

Docs-only query hint (not executed):
    from:subscription-alerts@marketrebellion.example.invalid newer_than:90d
"""

from __future__ import annotations


class LiveGmailPollerDisabled(NotImplementedError):
    """Raised if anything tries to start a live mailbox poller."""


def live_gmail_poller_not_implemented(*_args, **_kwargs):
    raise LiveGmailPollerDisabled(
        "Live Gmail poller is a stub. Replay signal_intake/fixtures/shelf "
        "or drop normalized JSON onto FABIO_MR_QUEUE_PATH. No mailbox credentials."
    )
