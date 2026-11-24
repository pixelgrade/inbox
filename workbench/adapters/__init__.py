"""Registered ticket research provider for this mailbox."""

research = None


def register_research(provider):
    global research
    research = provider


def reset_research():
    """Clear process-wide registration between tests."""
    register_research(None)
