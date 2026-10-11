from __future__ import annotations


class EndpointNotProjected(Exception):
    """An edge references a node that is not (yet) in this workspace's graph. Retryable: the node's own projection may still be pending."""
