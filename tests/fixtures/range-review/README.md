# Range-review fixtures

The executable tests create temporary Git repositories so commit IDs remain
independent of this checkout. They cover these stable scenarios:

- a three-file change split into deterministic, non-overlapping file chunks;
- an unchanged base/head pair accepted only with `--allow-empty`;
- an invalid head that fails before an output artifact is created; and
- a single file section larger than `--max-chunk-bytes` that fails closed.

The manifest stores each frozen patch as base64 alongside its SHA-256 digest.
The next range-review phase must consume those bytes rather than recomputing a
range after a branch advances.
