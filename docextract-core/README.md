# docextract-core

Shared substrate for the document-extraction packages ([`word-extract`](https://pypi.org/project/word-extract/)
and its siblings): content hashing, a strict canonical-JSON codec, archive helpers, a
content-addressed `Collection`, and a small LLM-client protocol. It has no runtime dependencies.

Most users want `word-extract`, which depends on this package.

Licensed under the Apache License, Version 2.0.
