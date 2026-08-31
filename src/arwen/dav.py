"""Thin, fully-typed wrapper over ``caldav``: If-Match, Schedule-Reply, and ETags.

This is the only module allowed to touch the ``caldav`` package directly. It
exposes typed domain objects to the rest of the code so that ``Any`` from the
untyped third-party surface never leaks further.
"""
