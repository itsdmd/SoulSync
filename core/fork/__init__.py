"""Personal-fork additions (itsdmd/SoulSync).

Everything the fork adds on top of upstream lives in this package (plus
``api/fork.py`` and ``webui/static/fork-ui.js``). Upstream files only carry
one-line calls into :mod:`core.fork.hooks`, so merging upstream stays cheap.
See ``FORK.md`` for the full list of touch points.
"""
