import time
from dataclasses import dataclass
from typing import Any

import frappe
import requests

MASKED_HEADER_KEYS = {"x-api-token", "authorization"}
REDACTED_VALUE = "***REDACTED***"


@dataclass
class RequestLogResult:
    response: requests.Response | None
    log_name: str
    error_type: str | None


def _mask_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Redact secret header values before they are persisted to Integration Request."""
    masked = dict(headers or {})
    for key in list(masked):
        if key.lower() in MASKED_HEADER_KEYS:
            masked[key] = REDACTED_VALUE
    return masked


def _parse_response_body(response: requests.Response) -> Any:
    try:
        return response.json()
    except (ValueError, TypeError):
        return response.text


def _fail(log, error_type: str, message: str, start: float) -> "RequestLogResult":
    duration = round(time.monotonic() - start, 3)
    log.db_set(
        "error",
        frappe.as_json(
            {"error_type": error_type, "message": message, "duration": duration},
            indent=1,
        ),
    )
    log.db_set("status", "Failed")
    frappe.log_error(
        frappe.get_traceback(), f"eShipz: {log.request_description} request failed"
    )
    return RequestLogResult(response=None, log_name=log.name, error_type=error_type)


def send_logged_request(
    *,
    method: str,
    url: str,
    request_description: str,
    reference_doctype: str,
    reference_docname: str,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    data: str | None = None,
    timeout: int = 30,
) -> RequestLogResult:
    """Send an HTTP request to a third-party carrier and persist an Integration
    Request record covering both the outbound request and the inbound response
    (or transport failure), for tracing eShipz API calls end-to-end.

    Never crashes on a malformed/non-JSON response body, and never commits —
    this always runs inside an already-open whitelisted request transaction.
    """
    request_body = data
    if request_body is None and json_body is not None:
        request_body = frappe.as_json(json_body, indent=1)

    log = frappe.get_doc(
        {
            "doctype": "Integration Request",
            "integration_request_service": "eShipz",
            "request_description": request_description,
            "status": "Queued",
            "url": url,
            "request_headers": frappe.as_json(_mask_headers(headers), indent=1),
            "data": request_body,
            "reference_doctype": reference_doctype,
            "reference_docname": reference_docname,
        }
    )
    log.insert(ignore_permissions=True)

    start = time.monotonic()
    try:
        response = requests.request(
            method, url, headers=headers, json=json_body, data=data, timeout=timeout
        )
    except requests.exceptions.Timeout as exc:
        return _fail(log, "Timeout", str(exc), start)
    except requests.exceptions.ConnectionError as exc:
        return _fail(log, "Connection Error", str(exc), start)
    except requests.exceptions.RequestException as exc:
        return _fail(log, "Request Exception", str(exc), start)

    duration = round(time.monotonic() - start, 3)
    log.db_set(
        "output",
        frappe.as_json(
            {
                "status_code": response.status_code,
                "duration": duration,
                "body": _parse_response_body(response),
            },
            indent=1,
        ),
    )
    log.db_set("status", "Completed")

    return RequestLogResult(response=response, log_name=log.name, error_type=None)


def record_log_error(
    log_name: str, error_message: str, error_type: str = "Response Parsing Error"
) -> None:
    """Flip an existing Integration Request (created by send_logged_request,
    currently status='Completed') to status='Failed' after the transport
    succeeded but domain-level parsing of the response body then failed.

    Commits immediately and mirrors the raw response into Error Log. Every
    caller of this function follows it with frappe.throw() to surface the
    failure to the user, but an unhandled exception from a whitelisted method
    rolls back the whole request's DB transaction (frappe/app.py's exception
    handler) -- which would otherwise silently erase this Integration Request
    (its insert, its output, and this failure status) moments after writing
    it, before anyone could ever look at the carrier's actual reply.
    """
    raw_output = frappe.db.get_value("Integration Request", log_name, "output")

    frappe.db.set_value(
        "Integration Request",
        log_name,
        {
            "status": "Failed",
            "error": frappe.as_json(
                {"error_type": error_type, "message": error_message}, indent=1
            ),
        },
    )
    frappe.log_error(
        title=f"eShipz {error_type} ({log_name})",
        message="%s\n\nRaw response (Integration Request %s):\n%s"
        % (error_message, log_name, raw_output),
    )
    frappe.db.commit()
