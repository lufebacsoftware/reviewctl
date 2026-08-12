# ADR 0001: Append-only journals and rebuildable projections

- Status: Accepted
- Date: 2026-08-12

## Context

Review receipts, findings, adjudications, fixes, waivers, verification results,
and model qualifications change interpretation over time. Mutable “current”
rows erase the sequence needed to explain why a merge or capability decision was
valid at a particular point.

## Decision

Project and organization evidence use append-only journals as their canonical
write model. Appends compare the expected sequence and previous-event digest
atomically. Existing events are not updated or deleted through normal
interfaces. Corrections and lifecycle changes append new events linked to the
facts they supersede.

Readable projections are derived state. They may be dropped and replayed from a
named journal checkpoint. Large or sensitive content is stored as an
EvidenceBlob and referenced by digest, classification, encryption metadata, and
an authorized locator.

## Consequences

- Historical decisions remain explainable and tampering is detectable.
- Concurrent writers must serialize per origin journal; stale heads fail
  explicitly.
- Projection code must be deterministic and schema-version aware.
- Retention acts on private blobs and appends an audited disposition; it does
  not silently rewrite unrelated canonical metadata.
