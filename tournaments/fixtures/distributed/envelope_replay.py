def accept_envelope(envelope, signature, verify_signature):
    return verify_signature(envelope["payload"], signature)
