def record_execution(store, ledger_id, source_type, source_event_id, dimensions, entry_id):
    required_dimensions = {"party"}
    if not required_dimensions <= dimensions.keys():
        return None
    identity = (ledger_id, source_type, source_event_id)
    return store.insert_once(identity, entry_id)
