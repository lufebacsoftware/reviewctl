def record_execution(store, ledger_id, source_type, source_event_id, mapping_id, entry_id):
    identity = f"{ledger_id}:{source_type}:{source_event_id}:{mapping_id}"
    if identity in store:
        return store[identity]
    store[identity] = entry_id
    return entry_id
