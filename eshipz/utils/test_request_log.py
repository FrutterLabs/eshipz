import json
from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from eshipz.utils.request_log import record_log_error, send_logged_request


def _make_response(status_code=200, json_body=None, text=None):
    response = Mock(spec=requests.Response)
    response.status_code = status_code
    if text is not None:
        response.text = text
        response.json.side_effect = ValueError("not json")
    else:
        response.text = json.dumps(json_body or {})
        response.json.return_value = json_body or {}
    return response


class TestSendLoggedRequest(FrappeTestCase):
    def tearDown(self):
        frappe.db.rollback()

    def _call(self, **overrides):
        kwargs = {
            "method": "POST",
            "url": "https://app.eshipz.com/api/v1/create-shipments",
            "request_description": "Create Shipment",
            "reference_doctype": "User",
            "reference_docname": frappe.session.user,
            "headers": {
                "X-API-TOKEN": "super-secret-token",
                "Content-Type": "application/json",
            },
            "json_body": {"order_id": "SHIP-001"},
        }
        kwargs.update(overrides)
        return send_logged_request(**kwargs)

    @patch("eshipz.utils.request_log.requests.request")
    def test_success_response_creates_completed_log(self, mock_request):
        """Success: a 200 response persists status=Completed with the status code and
        body captured, and the response object is returned to the caller."""
        mock_request.return_value = _make_response(
            200, {"data": {"order_id": "SHIP-001"}}
        )

        result = self._call()

        self.assertIsNotNone(result.response)
        self.assertEqual(result.response.status_code, 200)
        log = frappe.get_doc("Integration Request", result.log_name)
        self.assertEqual(log.status, "Completed")
        output = frappe.parse_json(log.output)
        self.assertEqual(output["status_code"], 200)
        self.assertEqual(output["body"]["data"]["order_id"], "SHIP-001")

    @patch("eshipz.utils.request_log.requests.request")
    def test_non_200_response_still_marks_completed(self, mock_request):
        """Boundary: a carrier 4xx/5xx HTTP response is a transport success, not a
        transport failure — the log is still Completed and the response is returned
        for the caller to interpret."""
        mock_request.return_value = _make_response(422, {"message": "invalid address"})

        result = self._call()

        self.assertIsNotNone(result.response)
        log = frappe.get_doc("Integration Request", result.log_name)
        self.assertEqual(log.status, "Completed")
        self.assertEqual(frappe.parse_json(log.output)["status_code"], 422)

    @patch("eshipz.utils.request_log.requests.request")
    def test_non_json_response_body_does_not_crash(self, mock_request):
        """Boundary: a non-JSON response body (e.g. an HTML error page) must not raise
        inside the helper — it falls back to raw text and still logs Completed."""
        mock_request.return_value = _make_response(502, text="<html>Bad Gateway</html>")

        result = self._call()

        self.assertIsNotNone(result.response)
        log = frappe.get_doc("Integration Request", result.log_name)
        self.assertEqual(log.status, "Completed")
        body = frappe.parse_json(log.output)["body"]
        self.assertEqual(body, "<html>Bad Gateway</html>")

    @patch("eshipz.utils.request_log.requests.request")
    def test_timeout_marks_log_failed_with_timeout_error_type(self, mock_request):
        """Failure: requests.exceptions.Timeout marks the log Failed with
        error_type='Timeout' and returns response=None."""
        mock_request.side_effect = requests.exceptions.Timeout("read timed out")

        result = self._call()

        self.assertIsNone(result.response)
        self.assertEqual(result.error_type, "Timeout")
        log = frappe.get_doc("Integration Request", result.log_name)
        self.assertEqual(log.status, "Failed")
        self.assertEqual(frappe.parse_json(log.error)["error_type"], "Timeout")

    @patch("eshipz.utils.request_log.requests.request")
    def test_connection_error_marks_log_failed(self, mock_request):
        """Failure: requests.exceptions.ConnectionError marks the log Failed with
        error_type='Connection Error'."""
        mock_request.side_effect = requests.exceptions.ConnectionError(
            "connection refused"
        )

        result = self._call()

        self.assertIsNone(result.response)
        self.assertEqual(result.error_type, "Connection Error")
        log = frappe.get_doc("Integration Request", result.log_name)
        self.assertEqual(log.status, "Failed")

    @patch("eshipz.utils.request_log.requests.request")
    def test_generic_request_exception_marks_log_failed(self, mock_request):
        """Failure: any other requests.exceptions.RequestException marks the log
        Failed with error_type='Request Exception'."""
        mock_request.side_effect = requests.exceptions.RequestException(
            "too many redirects"
        )

        result = self._call()

        self.assertIsNone(result.response)
        self.assertEqual(result.error_type, "Request Exception")

    @patch("eshipz.utils.request_log.requests.request")
    def test_api_token_header_is_masked_in_persisted_log(self, mock_request):
        """Boundary/Security: the literal X-API-TOKEN value never appears in the
        persisted request_headers field."""
        mock_request.return_value = _make_response(200, {})

        result = self._call()

        log = frappe.get_doc("Integration Request", result.log_name)
        self.assertNotIn("super-secret-token", log.request_headers)

    @patch("eshipz.utils.request_log.requests.request")
    def test_reference_fields_persisted_verbatim(self, mock_request):
        """Success: reference_doctype/reference_docname and
        integration_request_service are stored verbatim for later traceability
        lookups."""
        mock_request.return_value = _make_response(200, {})

        result = self._call()

        log = frappe.get_doc("Integration Request", result.log_name)
        self.assertEqual(log.integration_request_service, "eShipz")
        self.assertEqual(log.reference_doctype, "User")
        self.assertEqual(log.reference_docname, frappe.session.user)


class TestRecordLogError(FrappeTestCase):
    def tearDown(self):
        frappe.db.rollback()

    def test_record_log_error_flips_completed_log_to_failed(self):
        """Success: an existing Completed log is updated to status=Failed with the
        given error message and error_type."""
        log = frappe.get_doc(
            {
                "doctype": "Integration Request",
                "integration_request_service": "eShipz",
                "status": "Completed",
                "reference_doctype": "User",
                "reference_docname": frappe.session.user,
            }
        ).insert(ignore_permissions=True)

        record_log_error(log.name, "Unexpected response shape from carrier: 'files'")

        log.reload()
        self.assertEqual(log.status, "Failed")
        error = frappe.parse_json(log.error)
        self.assertEqual(error["error_type"], "Response Parsing Error")
        self.assertIn("files", error["message"])
