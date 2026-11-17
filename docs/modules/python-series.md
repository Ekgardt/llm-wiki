# Python series gate

`scripts/python_series.py` runs before anything else in the nightly. It imports only the standard library so that a checkout already ahead of its environment can still repair itself and rerun on the supported Python.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
