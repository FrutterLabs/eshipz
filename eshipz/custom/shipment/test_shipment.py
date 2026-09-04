import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext.stock.doctype.shipment.test_shipment import (
    create_test_delivery_note,
    create_test_shipment,
)

from eshipz.custom.shipment.shipment import (
    _build_items,
    _build_parcels,
    _invoice_details,
    _line_gross,
    _verify_declared_value,
    create_shipment,
)
from eshipz.utils.request_log import RequestLogResult


def _dn_line(item_code, qty, rate, igst=0.0, hs_code=None, weight_per_unit=0.0, uom="NOS"):
    """A Delivery Note Item shaped like the Shopify-sourced lines in production:
    no item_tax_template, GST carried on the india_compliance per-line fields."""
    net_amount = rate * qty
    return {
        "item_code": item_code,
        "item_name": item_code + " description",
        "qty": qty,
        "uom": uom,
        "gst_hsn_code": hs_code,
        "rate": rate,
        "amount": net_amount,
        "net_amount": net_amount,
        "igst_amount": igst,
        "cgst_amount": 0.0,
        "sgst_amount": 0.0,
        "cess_amount": 0.0,
        "cess_non_advol_amount": 0.0,
        "total_weight": weight_per_unit * qty,
        "weight_uom": "KG",
    }


def _declared(items):
    """What eShipz derives from the payload: sum(quantity * unit price)."""
    return round(sum(i["quantity"] * i["price"]["amount"] for i in items), 2)


class _StubDeliveryNote(SimpleNamespace):
    """A Delivery Note stand-in. Deliberately not frappe._dict: that subclasses
    dict, so `.items` would resolve to dict.items() instead of the child table."""


def _selected_service():
    return json.dumps(
        {
            "vendor_id": "BD",
            "description": "Bluedart",
            "slug": "bluedart",
            "selected_service_type": "surface",
        }
    )


def _mock_log_result(status_code, json_body, log_name="test-log-001"):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body
    response.text = json.dumps(json_body)
    return RequestLogResult(response=response, log_name=log_name, error_type=None)


class TestShipmentCreateShipmentLogging(FrappeTestCase):
    def setUp(self):
        frappe.db.set_single_value("eShipz Settings", "api_token", "test-token")
        delivery_note = create_test_delivery_note()
        delivery_note.submit()
        self.shipment = create_test_shipment([delivery_note])

    def tearDown(self):
        frappe.db.rollback()

    @patch("eshipz.custom.shipment.shipment.record_log_error")
    @patch("eshipz.custom.shipment.shipment.send_logged_request")
    def test_malformed_response_logs_and_throws_clean_error(
        self, mock_send, mock_record_error
    ):
        """Failure: a 200 response missing result['data']['files']['label'] raises a
        clean frappe.throw referencing the Integration Request name (not a raw
        KeyError), and flips that log to Failed via record_log_error."""
        mock_send.return_value = _mock_log_result(200, {"data": {"files": {}}})

        with self.assertRaises(frappe.ValidationError) as ctx:
            create_shipment(self.shipment.name, _selected_service())

        self.assertIn("test-log-001", str(ctx.exception))
        mock_record_error.assert_called_once()
        self.assertEqual(mock_record_error.call_args[0][0], "test-log-001")

    @patch("eshipz.custom.shipment.shipment.send_logged_request")
    def test_well_formed_response_books_shipment(self, mock_send):
        """Success: a well-formed 200 response still updates every db_set field and
        returns the expected dict — regression guard proving the refactor didn't
        break the happy path."""
        mock_send.return_value = _mock_log_result(
            200,
            {
                "data": {
                    "files": {
                        "label": {
                            "label_meta": {"url": "http://label.url", "awb": "AWB123"}
                        }
                    },
                    "slug": "bluedart",
                    "status": "In Transit",
                    "service_type": "surface",
                    "order_id": "ORDER123",
                }
            },
        )

        result = create_shipment(self.shipment.name, _selected_service())

        self.assertEqual(result["awb_number"], "AWB123")
        self.shipment.reload()
        self.assertEqual(self.shipment.awb_number, "AWB123")
        self.assertEqual(self.shipment.status, "Booked")

    @patch("eshipz.custom.shipment.shipment.send_logged_request")
    def test_non_200_response_throws_with_log_reference(self, mock_send):
        """Boundary: a non-200 response throws referencing the log name and never
        attempts to parse result['data']."""
        mock_send.return_value = _mock_log_result(
            422, {"message": "invalid address"}, log_name="test-log-002"
        )

        with self.assertRaises(frappe.ValidationError) as ctx:
            create_shipment(self.shipment.name, _selected_service())

        self.assertIn("test-log-002", str(ctx.exception))


class TestShipmentDeclaredValue(FrappeTestCase):
    """The qty-squared regression: eShipz reads price.amount as a UNIT price and
    multiplies by quantity, so sending the ERPNext line amount (rate x qty)
    declared rate x qty**2 on every line with qty >= 2."""

    def test_qty_one_line_is_unchanged(self):
        """Boundary: with qty 1, amount == rate, so the old code was accidentally
        correct. The fix must not move this number."""
        items = _build_items([_dn_line("A", 1, 258.93, igst=16.02)], "IN", "INR")

        self.assertEqual(_declared(items), 274.95)

    def test_multi_qty_line_no_longer_squares_quantity(self):
        """Regression, order #112219: rate 698.41 x qty 12. The dispatched label
        declared 1,00,571.04 (= 8,380.92 x 12); it should be the 8,799.97 grand
        total, and must never again be the squared figure."""
        items = _build_items(
            [_dn_line("BARK-CB-300GM", 12, 698.41, igst=419.05)], "IN", "INR"
        )

        self.assertEqual(items[0]["price"]["amount"], 733.33)
        self.assertAlmostEqual(_declared(items), 8799.97, delta=0.05)
        self.assertNotAlmostEqual(_declared(items), 100571.04, delta=1.0)

    def test_mixed_quantity_order_reconciles_to_grand_total(self):
        """Regression, order #112891: 13 lines, five of them multi-qty. The
        dispatched label declared 18,582.79 against a real 7,033.49."""
        rows = [
            _dn_line("COOK-CHO-140GM", 1, 258.93, igst=16.02, hs_code="19053100"),
            _dn_line("COMB-031", 1, 699.15, igst=43.22),
            _dn_line("COMB-029", 1, 270.63, igst=16.74),
            _dn_line("CHIK-MAK-50GM", 1, 100.23, igst=6.20, hs_code="17049090"),
            _dn_line("CHIK-SC-45GM", 1, 100.23, igst=6.20),
            _dn_line("COMB-027", 2, 270.64, igst=33.46),
            _dn_line("BSS-4121", 4, 167.06, igst=41.30),
            _dn_line("BSS-5048", 4, 187.95, igst=46.46, hs_code="18069010"),
            _dn_line("COMB-100", 4, 304.06, igst=75.15),
            _dn_line("BARK-CB-90GM", 1, 229.71, igst=14.21, hs_code="18069010"),
            _dn_line("COMB-106", 1, 618.15, igst=38.20),
            _dn_line("FUD-CLS-145GM-001", 4, 292.38, igst=72.22),
            _dn_line("BSS-0083", 1, 0.0),
        ]

        items = _build_items(rows, "IN", "INR")

        self.assertEqual(len(items), 13)
        self.assertAlmostEqual(_declared(items), 7033.49, delta=0.10)
        self.assertNotAlmostEqual(_declared(items), 18582.79, delta=1.0)

    def test_declared_value_is_tax_inclusive(self):
        """The label carries the invoice value, not the taxable value: a 5% line
        declares 105.00, not the 100.00 net."""
        items = _build_items([_dn_line("A", 1, 100.0, igst=5.0)], "IN", "INR")

        self.assertEqual(_declared(items), 105.00)

    def test_mixed_gst_rates_keep_their_own_tax(self):
        """Each line grosses up by its own GST, not a blended document rate: a 5%
        line and an 18% line must not cross-subsidise."""
        rows = [
            _dn_line("FIVE", 1, 100.0, igst=5.0),
            _dn_line("EIGHTEEN", 1, 100.0, igst=18.0),
        ]

        items = _build_items(rows, "IN", "INR")

        self.assertEqual(items[0]["price"]["amount"], 105.00)
        self.assertEqual(items[1]["price"]["amount"], 118.00)

    def test_zero_quantity_line_does_not_divide_by_zero(self):
        items = _build_items([_dn_line("A", 0, 100.0, igst=5.0)], "IN", "INR")

        self.assertEqual(items[0]["price"]["amount"], 0.0)

    def test_zero_value_service_line_is_priced_at_zero(self):
        """SHIPPING CHARGES rides along on every Shopify Delivery Note at rate 0."""
        items = _build_items([_dn_line("BSS-0083", 1, 0.0)], "IN", "INR")

        self.assertEqual(_declared(items), 0.0)


class TestShipmentItemPayload(FrappeTestCase):
    def test_sku_is_the_item_code_not_the_uom(self):
        """The old key sent item.uom, so every label printed 'NOS' as the SKU."""
        items = _build_items([_dn_line("BARK-CB-300GM", 1, 100.0)], "IN", "INR")

        self.assertEqual(items[0]["sku"], "BARK-CB-300GM")

    def test_hs_code_passes_through(self):
        items = _build_items(
            [_dn_line("A", 1, 100.0, hs_code="19053100")], "IN", "INR"
        )

        self.assertEqual(items[0]["hs_code"], "19053100")

    def test_item_weight_uses_the_delivery_note_weight(self):
        """The old code added 1 for any non-Kg UOM, so a 5.076 kg line of 12 was
        declared as 1 kg."""
        items = _build_items(
            [_dn_line("A", 12, 100.0, weight_per_unit=0.423)], "IN", "INR"
        )

        self.assertEqual(items[0]["weight"]["value"], 0.423)

    def test_unknown_weight_uom_yields_zero_not_a_wrong_number(self):
        row = _dn_line("A", 1, 100.0, weight_per_unit=2.0)
        row["weight_uom"] = "Furlong"

        items = _build_items([row], "IN", "INR")

        self.assertEqual(items[0]["weight"]["value"], 0.0)

    def test_gram_weights_convert_to_kg(self):
        row = _dn_line("A", 1, 100.0)
        row["total_weight"] = 423.0
        row["weight_uom"] = "Gram"

        items = _build_items([row], "IN", "INR")

        self.assertEqual(items[0]["weight"]["value"], 0.423)

    def test_duplicate_lines_sum_quantity_as_well_as_value(self):
        """The old key held qty and amount, so duplicate lines summed the amount
        while keeping one line's quantity — declaring double."""
        rows = [_dn_line("X", 2, 100.0, igst=10.0), _dn_line("X", 2, 100.0, igst=10.0)]

        items = _build_items(rows, "IN", "INR")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quantity"], 4)
        self.assertEqual(_declared(items), 420.00)

    def test_same_item_at_different_rates_stays_separate(self):
        rows = [_dn_line("X", 1, 100.0), _dn_line("X", 1, 250.0)]

        self.assertEqual(len(_build_items(rows, "IN", "INR")), 2)

    def test_distinct_items_sharing_a_name_are_not_merged(self):
        """Consolidation keys on item_code; the old key used item_name, so two
        different SKUs with the same name collapsed into one line."""
        a = _dn_line("COMB-029", 1, 270.63)
        b = _dn_line("CHIK-SC-45GM", 1, 100.23)
        a["item_name"] = b["item_name"] = "Super Crunch Chikki (45g)"

        self.assertEqual(len(_build_items([a, b], "IN", "INR")), 2)


class TestShipmentDeclaredValueTripwire(FrappeTestCase):
    def test_matching_grand_total_passes(self):
        rows = [_dn_line("A", 4, 100.0, igst=20.0)]
        items = _build_items(rows, "IN", "INR")
        dns = [_StubDeliveryNote(grand_total=420.0)]

        self.assertEqual(_verify_declared_value(items, rows, dns), 420.0)

    def test_grand_total_mismatch_blocks_the_booking(self):
        """A document-level discount or charge leaves the lines out of step with
        the grand total; better to stop than to print a wrong label."""
        rows = [_dn_line("A", 4, 100.0, igst=20.0)]
        items = _build_items(rows, "IN", "INR")
        dns = [_StubDeliveryNote(grand_total=999.0)]

        with self.assertRaises(frappe.ValidationError) as ctx:
            _verify_declared_value(items, rows, dns)

        self.assertIn("Declared value check failed", str(ctx.exception))

    def test_corrupted_unit_price_is_caught(self):
        """The guard that would have caught the original bug: if the payload ever
        drifts from the Delivery Note lines again, booking stops."""
        rows = [_dn_line("A", 4, 100.0, igst=20.0)]
        items = _build_items(rows, "IN", "INR")
        items[0]["price"]["amount"] = 420.0  # the old rate x qty behaviour
        dns = [_StubDeliveryNote(grand_total=420.0)]

        with self.assertRaises(frappe.ValidationError):
            _verify_declared_value(items, rows, dns)

    def test_client_supplied_items_skip_the_grand_total_cross_check(self):
        """item_data may cover a subset of the shipment, so only the internal
        consistency check applies."""
        rows = [_dn_line("A", 4, 100.0, igst=20.0)]
        items = _build_items(rows, "IN", "INR")
        dns = [_StubDeliveryNote(grand_total=999.0)]

        self.assertEqual(
            _verify_declared_value(items, rows, dns, cross_check_grand_total=False),
            420.0,
        )


class TestShipmentParcelAndInvoice(FrappeTestCase):
    def _doc(self, parcels):
        return frappe._dict(
            description_of_content="Food Items",
            shipment_type="Goods",
            shipment_parcel=parcels,
            value_of_goods=105.0,
        )

    def _parcel(self, idx=1):
        return frappe._dict(idx=idx, count=1, weight=10.3, width=32.0, height=16.5, length=39.0)

    def test_single_parcel_carries_the_items_and_order_value(self):
        dn = _StubDeliveryNote(items=[_dn_line("A", 1, 100.0, igst=5.0)], grand_total=105.0)
        doc = self._doc([self._parcel()])

        parcels, declared = _build_parcels(doc, [dn], None, "IN", "INR")

        self.assertEqual(len(parcels), 1)
        self.assertEqual(declared, 105.0)
        self.assertEqual(parcels[0]["order_value"], 105.0)
        self.assertEqual(len(parcels[0]["items"]), 1)

    def test_multiple_parcels_are_refused(self):
        """The old code attached the full item list to every parcel, declaring the
        order value once per parcel. eShipz supports one."""
        dn = _StubDeliveryNote(items=[_dn_line("A", 1, 100.0, igst=5.0)], grand_total=105.0)
        doc = self._doc([self._parcel(1), self._parcel(2)])

        with self.assertRaises(frappe.ValidationError) as ctx:
            _build_parcels(doc, [dn], None, "IN", "INR")

        self.assertIn("one parcel", str(ctx.exception))

    def test_invoice_details_come_from_the_delivery_note(self):
        """These orders are never billed in ERPNext, so there is no Sales Invoice
        to reference — the Delivery Note is the document that travels."""
        dn = frappe._dict(
            name="DN-26-01394",
            posting_date="2026-08-11",
            grand_total=8799.97,
            currency="INR",
            ewaybill=None,
        )

        numbers, dates, gst_invoices, currency = _invoice_details([dn])

        self.assertEqual(numbers, ["DN-26-01394"])
        self.assertEqual(dates, ["2026-08-11"])
        self.assertEqual(currency, "INR")
        self.assertEqual(gst_invoices[0]["invoice_number"], "DN-26-01394")
        self.assertEqual(gst_invoices[0]["invoice_value"], 8799.97)

    def test_mixed_currency_shipment_is_refused(self):
        """invoice_currency used to default silently to INR, so a non-INR order
        would have been mislabelled with no error."""
        dns = [
            frappe._dict(name="A", posting_date="2026-08-11", grand_total=1.0, currency="INR", ewaybill=None),
            frappe._dict(name="B", posting_date="2026-08-11", grand_total=1.0, currency="USD", ewaybill=None),
        ]

        with self.assertRaises(frappe.ValidationError) as ctx:
            _invoice_details(dns)

        self.assertIn("mixes currencies", str(ctx.exception))


class TestLineGross(FrappeTestCase):
    def test_all_gst_components_are_counted(self):
        row = _dn_line("A", 1, 100.0)
        row.update(
            {
                "cgst_amount": 2.5,
                "sgst_amount": 2.5,
                "cess_amount": 1.0,
                "cess_non_advol_amount": 0.5,
            }
        )

        self.assertEqual(_line_gross(row), 106.5)

    def test_missing_tax_fields_are_treated_as_zero(self):
        self.assertEqual(_line_gross({"net_amount": 100.0}), 100.0)
