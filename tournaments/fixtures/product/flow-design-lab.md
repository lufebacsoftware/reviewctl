# Flow Design Lab

Design a product for operators who need to model a business event before accounting execution.

The product must let an operator define mappings as data, attach versioned fixtures, simulate a
proposed accounting document, inspect a deterministic explanation, and explicitly approve a version.
Only an approved mapping may receive a runtime event. Runtime execution creates an idempotent posting
receipt and an outbox record for later provider work.

## Non-negotiable constraints

- `no-clojure-runtime`: take useful ideas from declarative data design without adding a Clojure runtime.
- `simulation-no-side-effects`: simulation must not create postings, provider commands, or external effects.
- `mapping-version-evidence`: a mapping has a stable version, hash, and executable fixture evidence.
- `posted-records-immutable`: posted accounting records are reversed, never edited in place.
- `execution-idempotent`: one business event cannot create two posting receipts.
