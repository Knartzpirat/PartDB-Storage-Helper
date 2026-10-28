"""Lokales Lagerbox- und Label-Tool für Part-DB + Brother P-touch."""
try:
    from ._version import VERSION as __version__   # Build-Version (Git-Tag v1.2.3 -> 1.2.3, sonst 0.0.0-<sha>)
except Exception:                                   # z. B. Helfer-ZIP ohne _version.py
    __version__ = "0.0.0-dev"
if not isinstance(__version__, str) or not __version__.strip():
    __version__ = "0.0.0-dev"
