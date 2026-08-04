# Invoice and Inventory Document

Design an application flow where an invoice contains service items and stocked goods. Stocked goods may
consume lots and create cost-of-goods effects. Services create revenue effects only. The user needs a
draft document, validation before approval, and an explainable preview of the accounting consequences.

The accounting mapping belongs to the approved document version. Inventory ownership, lot availability,
and accounting posting must have explicit boundaries.

## Non-negotiable constraints

- `document-before-posting`: an accounting consequence follows an approved business document, not direct UI mutation.
- `inventory-owned-separately`: inventory availability and lots have an explicit owner and consistency boundary.
- `mapping-version-evidence`: the mapping and input document versions are retained with the result.
- `posted-records-immutable`: corrections use a new document or reversal, never direct mutation.
