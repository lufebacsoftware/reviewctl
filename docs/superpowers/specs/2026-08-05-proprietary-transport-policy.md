# Proprietary Transport Policy

## Problem

`reviewctl` records a policy decision per model through `source_allowed`, but
the runner rejects proprietary packets for the direct OpenRouter and native
Antigravity transports before it reads that decision. This makes an approved
model unusable and turns an organization-owned policy into a hard-coded
provider ban.

## Decision

For a proprietary packet, `source_allowed = true` authorizes the configured
model on every built-in review transport: `llm`, `openrouter`, `agy`, and
`codex`.

The default remains deny: a missing policy, a missing model entry, or
`source_allowed = false` rejects the packet before an attempt is created. The
existing proprietary safeguards remain required:

- `--response-contract findings-json`
- an explicit policy file and its digest in the receipt
- a persisted request/response attempt receipt
- rejection of empty or invalid provider responses

## Compatibility

Existing policies need no changes. A policy that already contains
`source_allowed = true` will begin to authorize its model for OpenRouter and
Antigravity in addition to the transports it already authorizes. A later,
stricter transport allow-list can be added as an explicit policy extension;
it is not introduced now because it would keep current review work blocked.

## Verification

Tests will prove:

1. denied proprietary models still return code 3 before artifacts or provider
   calls;
2. a permitted proprietary OpenRouter packet invokes the transport and records
   its receipt;
3. a permitted proprietary Antigravity packet invokes the transport and
   records its receipt;
4. the full test suite passes.
