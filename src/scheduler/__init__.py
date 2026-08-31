"""View materialization scheduler.

Given a Unity Catalog view, work out the earliest time each day its
materialization can run with every dependency already refreshed.

READ ONLY. This package never writes, alters, optimizes, vacuums or drops a
user table. Metadata and history reads only.
"""
