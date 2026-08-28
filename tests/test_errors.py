from reviewctl.errors import Diagnostic, exit_code_for


def test_diagnostic_is_safe_machine_readable() -> None:
    diagnostic = Diagnostic(
        code="privacy_denied",
        message="private project requires an explicit packet",
        retryable=False,
        next="select files",
    )

    assert diagnostic.to_dict() == {
        "code": "privacy_denied",
        "message": "private project requires an explicit packet",
        "retryable": False,
        "next": "select files",
        "artifacts": [],
    }
    assert exit_code_for("privacy_denied") == 4
    assert exit_code_for("receipt_invalid") == 5
