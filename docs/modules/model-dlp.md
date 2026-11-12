# Model data-loss prevention

`scripts/model_dlp.py` is the one fail-closed policy applied to every first-party model call. Its dependencies are redaction and canonical bytes; the provider call itself is not owned here.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
