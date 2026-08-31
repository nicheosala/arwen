"""Pre-mutation ``.ics`` writer.

Writes every resource about to be deleted or modified to a single backup
file before any mutating request is sent. Kept general enough for reuse by
the future ``backup``/``restore`` commands.
"""
