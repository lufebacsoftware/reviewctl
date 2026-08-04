# Posted Entry Correction Request

A stakeholder asks for a button that directly edits a posted journal entry because an account and amount
were wrong. They insist the original record should disappear so reports look clean.

Design the product response. It must preserve auditability while remaining usable for an accountant who
made an honest mistake.

## Non-negotiable constraints

- `posted-records-immutable`: a posted entry cannot be edited or deleted.
- `correction-traceable`: a correction links to the original and explains the reason.
- `reports-auditable`: reports can distinguish original, reversal, and replacement effects.
