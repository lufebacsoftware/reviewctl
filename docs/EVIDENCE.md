# Evidence Contract

## Receipt contents

`receipt.json` is canonical JSON and contains the review ID, base source provenance, file SHA-256
digests, prompt digest, policy digest, requested and resolved models, attempt outcomes, token counts,
latency, provider cost when available, structured findings, and a receipt digest.

`reviewctl verify` recomputes the receipt digest. A rebase, altered finding, or altered source
provenance invalidates the prior receipt and requires a fresh review. The policy digest is the exact
policy bytes used for that historical decision: changing the policy later does not alter an existing
receipt, but a new review records the new policy digest.

## Sealed audit payload

The runner never copies source files into its artifact directory. It writes private temporary snapshots
only for the lifetime of the model process; receipt provenance hashes those exact bytes. Passing
`--seal-to` stores the exact assembled request and raw model response only as Age-encrypted payloads.
SQLite transport databases are deleted after extraction, including when Age sealing fails, because they
contain raw prompt and response data. The Codex transport similarly deletes its `--output-last-message`
file and any temporary JSON schema after extraction; the receipt keeps the Codex session identifier,
response hash, and validated structured findings.

For proprietary Codex reviews on macOS, the runner also creates a temporary `CODEX_HOME` with only the
authentication file required by Codex and applies a `sandbox-exec` deny rule to every original review
source root. This proves that Codex cannot reopen the reviewed checkout after snapshotting. It is not a
whole-host sandbox claim: system paths outside the source roots are outside this narrow boundary.

On timeout, `reviewctl` discards any partially written Codex final message before validation. A timeout
can record duration and transport failure, but it can never contribute a partial verdict or finding to a
receipt.

The structured findings contract accepts exactly two outcomes: `approved` with no findings, or
`changes-requested` with one or more findings. A model that reports unavailable context, refuses to
read the files, or returns an ambiguous natural-language verdict produces an unavailable receipt rather
than an approval.

Every structured response must also return a SHA-256 for every frozen review file. `reviewctl` compares
that set exactly with the locally snapshotted bytes. This is not a proof of model reasoning, but it is a
machine-checkable proof that the response could not have been accepted without correctly reading each
bounded file.

Evidence repositories retain the visible receipt and sealed payloads. Their commits must be signed by
the owning organization. Release signing and reproducible-build provenance apply to the `reviewctl`
distribution itself, not to a model's opinion.

## Retention and sharing

The organization defines retention in its evidence repository. Cross-organization syntheses should use
an abstract taxonomy, for example `idempotency uniqueness violated`, never source snippets or business
identifiers. A human approves every vault entry after secret and PII scanning.
