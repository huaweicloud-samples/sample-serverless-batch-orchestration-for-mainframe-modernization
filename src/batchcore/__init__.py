"""The batch core of this solution.

Three layers sit in this package: the validation answers, the orchestration
pass and the shared access layer they both stand on. They are deployed as two
code archives, each with its own entry module; the archives are built by
``scripts/package_functions.py``.

The tests under ``tests`` are for local checking only and are never put into a
deployed archive.
"""
