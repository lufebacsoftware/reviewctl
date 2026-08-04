def claim_next(connection):
    row = connection.execute("SELECT id FROM outbox WHERE delivered_at IS NULL LIMIT 1").fetchone()
    if row:
        connection.execute(
            "UPDATE outbox SET claimed_at = CURRENT_TIMESTAMP WHERE id = ?", (row[0],)
        )
    return row
