#!/usr/bin/env python3
"""
cloud_cartography_network_visualizer.py
=======================================

A tiny, dependency‑free generator that turns a list of Farcaster usernames
into two JSON files ready for the in‑browser visualizer:

* ``users.json`` – an array of node objects (``id`` & ``username``)
* ``events.json`` – an array of edge objects with a ``time`` stamp
  (``source``, ``target``, ``time`` and optional ``type``)

The script is deliberately simple, reproducible and suitable for
production pipelines:

* Input can be supplied either as command‑line arguments or via a plain‑text
  file (one username per line).
* The random seed can be fixed for deterministic output.
* All timestamps lie in the interval ``[0, 10]`` (the timeline required
  by the spec) and are uniformly distributed.
* The number of events is proportional to the number of users (default:
  ``max_edges = n * (n‑1) // 4``) but can be overridden.

Example usage
-------------