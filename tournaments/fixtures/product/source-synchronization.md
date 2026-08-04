# External Source Synchronization

Design a personal-finance core that synchronizes with an external application which is the user-facing
source of transaction edits. The core needs correct double-entry records while the external source has
its own identifiers, categories, and change history. Synchronization is bidirectional and can be
replayed after interruption.

The integration must explain identity translation, conflict handling, provenance, and what becomes a
new accounting correction instead of a destructive update.

## Non-negotiable constraints

- `external-identity-preserved`: external IDs and source provenance remain recoverable.
- `sync-idempotent`: replaying a source change does not duplicate a financial effect.
- `posted-records-immutable`: posted records are corrected through compensating records.
- `ownership-explicit`: the external system and the accounting core have explicit ownership boundaries.
