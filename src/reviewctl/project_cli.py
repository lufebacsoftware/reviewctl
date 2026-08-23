"""Project-oriented command line entry points."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from reviewctl.api import ReviewClient, ReviewRequest, ReviewResult
from reviewctl.config import ReviewConfig, load_config
from reviewctl.errors import Diagnostic, exit_code_for
from reviewctl.pi_transport import PiTransport

PROJECT_TEMPLATE = '''# Project-local reviewctl configuration.
# Keep the project private by default. Change privacy_mode to "sensitive" to
# require local execution for every profile.
[project]
visibility = "private"
privacy_mode = "private"

[profiles.default]
routes = ["pi:openrouter/stealth/ox-alpha"]
response_contract = "findings-json"
execution = "remote"
tools = "none"
timeout_seconds = 300
max_output_tokens = 8000
'''
FINDING_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _project_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _json_default(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=True, indent=2, default=_json_default, sort_keys=True))


def _result_payload(result: ReviewResult) -> dict[str, Any]:
    receipt = str(result.receipt_path)
    if receipt == ".":
        receipt = ""
    payload: dict[str, Any] = {
        "status": result.status,
        "reviewId": result.review_id,
        "receipt": receipt or None,
        "findings": [asdict(finding) for finding in result.findings],
    }
    if result.diagnostic is not None:
        payload["diagnostic"] = result.diagnostic.to_dict()
    return payload


def _print_result(result: ReviewResult, output_format: str, *, stream: Any = None) -> None:
    if stream is None:
        stream = sys.stdout
    if output_format == "json":
        print(
            json.dumps(
                _result_payload(result),
                ensure_ascii=True,
                indent=2,
                default=_json_default,
                sort_keys=True,
            ),
            file=stream,
        )
        return
    print(f"status: {result.status}", file=stream)
    if result.review_id:
        print(f"review: {result.review_id}", file=stream)
    if str(result.receipt_path) != ".":
        print(f"receipt: {result.receipt_path}", file=stream)
    for finding in result.findings:
        location = finding.path or "(project)"
        if finding.line is not None:
            location += f":{finding.line}"
        print(f"[{finding.severity}] {location} — {finding.title}", file=stream)
    if result.diagnostic is not None:
        diagnostic = result.diagnostic
        print(f"diagnostic: {diagnostic.code}: {diagnostic.message}", file=stream)
        if diagnostic.next:
            print(f"next: {diagnostic.next}", file=stream)


def _diagnostic_result(diagnostic: Diagnostic, output_format: str) -> int:
    result = ReviewResult(
        status=diagnostic.code,
        review_id="",
        receipt_path=Path(),
        findings=(),
        diagnostic=diagnostic,
    )
    _print_result(
        result,
        output_format,
        stream=sys.stderr if output_format == "text" else sys.stdout,
    )
    return exit_code_for(diagnostic.code)


def init_project(args: Any) -> int:
    project = _project_path(args.project)
    if project.exists() and not project.is_dir():
        return _diagnostic_result(
            Diagnostic("invalid_request", f"project path is not a directory: {project}"),
            "text",
        )
    project.mkdir(parents=True, exist_ok=True)
    config = project / "reviewctl.toml"
    if config.exists() and not args.force:
        return _diagnostic_result(
            Diagnostic(
                "invalid_request",
                f"configuration already exists: {config}",
                next="use --force only after inspecting the existing file",
            ),
            "text",
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    template = PROJECT_TEMPLATE.replace(
        'privacy_mode = "private"', f'privacy_mode = "{args.mode}"'
    )
    if args.mode == "sensitive":
        template = template.replace(
            'routes = ["pi:openrouter/stealth/ox-alpha"]', "routes = []"
        ).replace('execution = "remote"', 'execution = "local"')
    descriptor = os.open(config, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, template.encode("utf-8"))
    finally:
        os.close(descriptor)
    print(config)
    return 0


def review_project(args: Any) -> int:
    try:
        prompt = (
            Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
            if args.prompt_file
            else args.prompt
        )
        client = ReviewClient.from_project(_project_path(args.project))
    except (OSError, UnicodeError, ValueError) as error:
        return _diagnostic_result(Diagnostic("invalid_request", str(error)), args.format)
    result = client.review(
        ReviewRequest(
            prompt=prompt,
            files=tuple(Path(value).expanduser() for value in args.files),
            profile=args.profile,
            review_id=args.review_id,
        )
    )
    _print_result(result, args.format)
    if result.status == "accepted":
        if args.fail_on is not None and any(
            FINDING_SEVERITY_RANK.get(finding.severity, 0)
            >= FINDING_SEVERITY_RANK[args.fail_on]
            for finding in result.findings
        ):
            return 1
        return 0
    if result.diagnostic is not None:
        return exit_code_for(result.diagnostic.code)
    return exit_code_for(result.status)


def _status_payload(config: ReviewConfig, client: ReviewClient) -> dict[str, Any]:
    events, diagnostic = client.journal().read_with_diagnostic()
    review_ids = {event.get("reviewId") for event in events if event.get("reviewId")}
    payload: dict[str, Any] = {
        "project": config.project.name,
        "visibility": config.project.visibility,
        "privacyMode": config.project.privacy_mode,
        "profiles": sorted(config.profiles),
        "journal": {
            "events": len(events),
            "reviews": len(review_ids),
            "findings": len([event for event in events if event.get("type") == "finding"]),
        },
    }
    if diagnostic is not None:
        payload["diagnostic"] = diagnostic.to_dict()
    return payload


def status_project(args: Any) -> int:
    try:
        client = ReviewClient.from_project(_project_path(args.project))
    except ValueError as error:
        return _diagnostic_result(Diagnostic("config_invalid", str(error)), args.format)
    payload = _status_payload(client.config, client)
    if args.format == "json":
        _print_json(payload)
    else:
        print(f"project: {payload['project']}")
        print(f"privacy: {payload['privacyMode']}")
        print(f"reviews: {payload['journal']['reviews']}")
        print(f"findings: {payload['journal']['findings']}")
    diagnostic = payload.get("diagnostic")
    return exit_code_for(diagnostic["code"]) if isinstance(diagnostic, dict) else 0


def findings_project(args: Any) -> int:
    try:
        client = ReviewClient.from_project(_project_path(args.project))
    except ValueError as error:
        return _diagnostic_result(Diagnostic("config_invalid", str(error)), args.format)
    findings, diagnostic = client.journal().read_with_diagnostic()
    selected = [event for event in findings if event.get("type") == "finding"]
    if args.status:
        selected = [event for event in selected if event.get("status") == args.status]
    payload: object = selected
    if diagnostic is not None:
        payload = {"findings": selected, "diagnostic": diagnostic.to_dict()}
    if args.format == "json":
        _print_json(payload)
    else:
        for finding in selected:
            print(
                f"[{finding.get('severity', 'unknown')}] "
                f"{finding.get('path', '(project)')} — {finding.get('message', '')}"
            )
        if diagnostic is not None:
            print(f"diagnostic: {diagnostic.code}: {diagnostic.message}")
    return exit_code_for(diagnostic.code) if diagnostic is not None else 0


def _capability_payload() -> dict[str, object]:
    capabilities = PiTransport.capabilities()
    payload = {key: _json_default(value) for key, value in asdict(capabilities).items()}
    payload["output_token_limit_enforced"] = capabilities.output_token_limit_enforced
    return payload


def doctor_project(args: Any) -> int:
    try:
        config = load_config(_project_path(args.project))
    except ValueError as error:
        return _diagnostic_result(Diagnostic("config_invalid", str(error)), args.format)
    profiles = [
        {
            "name": profile.name,
            "routes": list(profile.routes),
            "responseContract": profile.response_contract,
            "execution": profile.execution,
            "tools": profile.tools,
            "timeoutSeconds": profile.timeout_seconds,
            "maxOutputTokens": profile.max_output_tokens,
        }
        for profile in config.profiles.values()
    ]
    payload = {
        "project": config.project.name,
        "visibility": config.project.visibility,
        "privacyMode": config.project.privacy_mode,
        "profiles": profiles,
        "transports": {
            "pi": {
                "executable": bool(shutil.which("pi")),
                "capabilities": _capability_payload(),
            }
        },
    }
    if args.format == "json":
        _print_json(payload)
    else:
        print(f"project: {config.project.name}")
        print(f"privacy: {config.project.privacy_mode}")
        for profile in profiles:
            print(f"profile {profile['name']}: {', '.join(profile['routes']) or '(no route)'}")
        print(f"pi executable: {'yes' if payload['transports']['pi']['executable'] else 'no'}")
    return 0


def add_project_commands(commands: Any) -> None:
    init = commands.add_parser("init", help="create a project-local reviewctl.toml")
    init.add_argument("--project", default=".")
    init.add_argument("--force", action="store_true")
    init.add_argument("--mode", choices=("personal", "private", "sensitive"), default="private")
    init.set_defaults(handler=init_project)

    review = commands.add_parser("review", help="run one project-scoped review")
    review.add_argument("--project", default=".")
    review.add_argument("--profile", default="default")
    prompt = review.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    review.add_argument("--file", dest="files", action="append", default=[])
    review.add_argument("--review-id")
    review.add_argument("--format", choices=("text", "json"), default="text")
    review.add_argument("--fail-on", choices=tuple(FINDING_SEVERITY_RANK), default=None)
    review.set_defaults(handler=review_project)

    status = commands.add_parser("status", help="show project review status")
    status.add_argument("--project", default=".")
    status.add_argument("--format", choices=("text", "json"), default="text")
    status.set_defaults(handler=status_project)

    findings = commands.add_parser("findings", help="show findings from the project journal")
    findings.add_argument("--project", default=".")
    findings.add_argument("--status")
    findings.add_argument("--format", choices=("text", "json"), default="text")
    findings.set_defaults(handler=findings_project)

    doctor = commands.add_parser("doctor", help="inspect safe local review configuration")
    doctor.add_argument("--project", default=".")
    doctor.add_argument("--format", choices=("text", "json"), default="text")
    doctor.set_defaults(handler=doctor_project)
