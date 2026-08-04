# Payment Lifecycle and Reconciliation

Design the product behavior for a business that accepts a customer funding instruction, reserves value,
submits to a provider, receives an asynchronous provider confirmation, and reconciles settlement.

The provider can send duplicate and out-of-order webhooks. A provider confirmation is evidence, not a
reason to mutate a posted journal entry. The service must explain the difference between an internal
intent, a provider attempt, a settlement fact, and a reconciliation result.

## Non-negotiable constraints

- `provider-evidence-immutable`: provider evidence is append-only and attributable to the provider event.
- `reserve-before-submit`: funds availability is checked and reserved before provider submission.
- `webhook-idempotent`: duplicate provider events do not repeat accounting or settlement effects.
- `reconciliation-separate`: reconciliation compares facts; it does not overwrite historical facts.
