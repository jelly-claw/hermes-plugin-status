"""Status messenger plugin — registers the Status gateway platform adapter.

User-dir plugin (``~/.hermes/plugins/status/``) so it survives Hermes upgrades,
which rewrite the bundled ``hermes-agent/`` tree. The loader calls ``register``;
see ``adapter.py`` for the implementation.
"""

from .adapter import register

__all__ = ["register"]
