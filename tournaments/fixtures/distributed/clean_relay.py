def relay_message(store, message, deliver):
    claim = store.claim_once(message["id"], message["lease_expires_at"])
    if claim is None:
        return None
    evidence = deliver(message)
    store.record_provider_evidence(message["id"], evidence)
    return store.mark_settled(message["id"], evidence["reconciliation_id"])
