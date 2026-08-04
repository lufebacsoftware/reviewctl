def append_audit(connection, receipt_id, event_hash):
    return connection.execute(
        "INSERT INTO audit_event (receipt_id, event_hash) VALUES (?, ?)",
        (receipt_id, event_hash),
    )
