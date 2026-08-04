def insert_once(connection, ledger_id, source_type, source_event_id, payload_hash):
    return connection.execute(
        """
        INSERT INTO receipt (ledger_id, source_type, source_event_id, payload_hash)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (ledger_id, source_type, source_event_id) DO NOTHING
        """,
        (ledger_id, source_type, source_event_id, payload_hash),
    )
