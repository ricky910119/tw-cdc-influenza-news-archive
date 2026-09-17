"""Discoverable-news archiver for Taiwan CDC / influenza coverage.

This package collects the set of news articles that a given search provider
can surface for a given set of queries, over a given date range, at a given
moment. It does not, and cannot, collect "all news on the internet".
"""

__version__ = "0.1.0"

SCHEMA_VERSION = 1
DB_SCHEMA_VERSION = 1

__all__ = ["__version__", "SCHEMA_VERSION", "DB_SCHEMA_VERSION"]
