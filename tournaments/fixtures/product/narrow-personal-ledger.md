# Narrow Personal Ledger

Design a first release for one person recording cash, bank, and card activity with a small chart of
accounts. They need drafts, balanced entries, a journal, and balances. They do not need provider rails,
inventory, multi-organization workflow, or distributed orchestration in the first release.

## Non-negotiable constraints

- `commodity-explicit`: every monetary amount has an explicit commodity.
- `balanced-before-posting`: a draft may be incomplete, but a posted entry is balanced.
- `scope-small`: the first release must not introduce provider rails or distributed workflow.
