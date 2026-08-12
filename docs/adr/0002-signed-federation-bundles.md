# ADR 0002: Federation through signed bundles

- Status: Accepted
- Date: 2026-08-12

## Context

Projects and organizations need compatible aggregate review evidence without
granting peers database access or exporting proprietary source, prompts, raw
responses, credentials, or internal identifiers.

## Decision

Federation exports signed, schema-versioned, allow-listed facts from a
profile-specific FederationExportJournal. A bundle identifies its origin,
authorized signing key, export profile, sequence range and checkpoints,
individual fact identities and digests, dimensions, mappings, classifications,
and supersessions or suppressions.

Import is idempotent per origin and item identity. Reusing an identity with
different bytes, hiding a published-stream gap or fork, using an unauthorized
key, or supplying incompatible dimensions is rejected. Importers pin an origin
trust manifest and evaluate signing authority at signing time plus current
revocation policy.

## Consequences

- Peers exchange evidence facts, not access to canonical private stores.
- Aggregates remain reproducible to accepted bundles and mapping paths.
- Stream continuity does not imply completeness of a private project or
  qualification journal.
- Corrections and retention propagate as signed supersession or suppression
  facts rather than mutation of imported history.
