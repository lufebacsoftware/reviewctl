def record_posting(connection, ledger_id, event_id, mapping_id, payload):
    return connection.execute(
        "INSERT INTO postings (ledger_id, event_id, mapping_id, payload) VALUES (?, ?, ?, ?)",
        (ledger_id, event_id, mapping_id, payload),
    )
