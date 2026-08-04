def accept_webhook(headers, body, verify_signature):
    return verify_signature(headers["X-Signature"], body)
