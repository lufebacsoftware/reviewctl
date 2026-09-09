# Test Layers

`reviewctl` separates deterministic implementation checks from environment and
provider checks. A receipt is evidence of one model invocation; it is neither a
test result nor merge approval by itself.

## Deterministic PR gate

Every deterministic test is classified as `unit` or `contract`. Pull requests
run both classes on Ubuntu with a locked dependency set, coverage, JUnit output,
and the twenty slowest test durations:

```bash
uv run pytest -m "unit or contract" \
  --junitxml=test-results/junit.xml --durations=20 \
  --cov=reviewctl --cov-branch --cov-report=term-missing
```

`unit` tests exercise local parsing, policy, receipt, and filesystem behavior.
`contract` tests exercise CLI boundaries, frozen packets, fake provider
executables, process lifecycle, and GitHub-facing adapters without using an
account, network provider, or repository secret.

## Synthetic canary

The scheduled and manual `Synthetic canary` workflow invokes the installed
controller through a disposable fake `llm` executable, writes a receipt, and
runs `reviewctl verify` on it. It exercises the end-to-end receipt path without
provider credentials, source transmission, or model spend.

Run it locally with:

```bash
uv run python scripts/synthetic_canary.py
```

This deterministic fake is distinct from a provider-backed profile canary. The
profile canary makes one real synthetic request through a user-configured
route, then writes `transport-canary.json` only when the resulting
`receipt.json` is valid:

```bash
reviewctl transport-canary --profile NAME
reviewctl verify /path/to/receipt.json
```

It is an operability observation for that profile at that time, not model
qualification or approval evidence. It never updates routes, policies, or
credentials.

## Platform and live checks

New tests that require an operating-system feature belong to `@pytest.mark.platform`.
Provider, account, repository, or secret-dependent checks belong to
`@pytest.mark.live`. They are deliberately excluded from PR CI. Run them only
with the explicit marker and record the environment, command, result, and any
receipt in the owning evidence repository.

```bash
uv run pytest -m platform
```

There are no generic `live` tests because provider credentials, acceptable
source boundaries, and expected outcomes belong to each organization's private
evidence store. No successful command alone establishes merge approval. Merge
approval requires the project policy's required tests and independently verified
review findings.
