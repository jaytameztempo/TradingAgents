"""HTTPS through the operating system's certificate store.

On a machine whose antivirus re-signs HTTPS traffic (Avast Web Shield, for one), Python's bundled certifi list
lacks that root, so every Alpaca and SEC request fails certificate verification. The system store trusts it.

Only the modules that make HTTPS requests call this (extensions.market_data and scanbot.fundamentals): it imports
requests, and the paper ticket and earnings steps must load no HTTP module at all.
"""

from __future__ import annotations


def use_system_trust_store() -> bool:
    """Verify certificates against the OS store from now on. False when truststore is not installed (certifi's
    list stays in use). Safe to call more than once."""
    try:
        import truststore
    except ImportError:
        return False
    truststore.inject_into_ssl()
    return True
