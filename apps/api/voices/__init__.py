"""VOICES — practitioner material about finding work and finding people (docs/specs/voices.md).

There is no `vo_*` schema. The corpus is the kernel's `rs_block` (text + tsvector + pgvector + jsonb
facets); this package is only the MODE on top of it — a search shaped as moments, an ingest runner,
and the routes. Everything here is keyword-first, so it works with no embedding provider at all.
"""
