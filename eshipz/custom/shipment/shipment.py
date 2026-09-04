import frappe
import json
from datetime import datetime
from frappe import _
from frappe.utils import flt

from eshipz.utils.request_log import record_log_error, send_logged_request


# eShipz reads `price.amount` on a parcel item as the price of ONE unit and
# multiplies it by `quantity`. Sending the ERPNext line `amount` (already
# rate x qty) therefore declared rate x qty**2 on every multi-qty line.
DECLARED_VALUE_TOLERANCE = 1.0

# `weight.value` is sent per unit, mirroring the confirmed `price.amount`
# behaviour. eShipz has not confirmed this field in writing; flip to False if
# they state it is the whole-line weight. Item weight does not affect billing --
# the carrier rates off the top-level `charged_weight`.
ITEM_WEIGHT_PER_UNIT = True

_WEIGHT_UOM_TO_KG = {
    "KG": 1.0,
    "KGS": 1.0,
    "KILOGRAM": 1.0,
    "GRAM": 0.001,
    "GRAMS": 0.001,
    "G": 0.001,
    "GM": 0.001,
    "MG": 0.000001,
    "TONNE": 1000.0,
}


def _flt(row, fieldname):
    """Read a numeric field off either a Delivery Note Item doc or a plain dict."""
    return flt(row.get(fieldname))


def _weight_in_kg(value, weight_uom):
    """Convert a Delivery Note Item weight to kg. Unknown UOMs yield 0 rather
    than a wrong number -- eShipz item weight is documentation only."""
    factor = _WEIGHT_UOM_TO_KG.get((weight_uom or "").strip().upper())
    return flt(value) * factor if factor else 0.0


def _line_gross(row):
    """Tax-inclusive value of one Delivery Note line.

    India Compliance stamps the GST split onto each line, so the gross is exact
    per line and sums to the Delivery Note grand total with no allocation step.
    item_tax_template is NOT usable here: it is unset on Shopify-sourced lines,
    which carry their tax as a document-level 'Actual' charge instead.
    """
    return (
        _flt(row, "net_amount")
        + _flt(row, "igst_amount")
        + _flt(row, "cgst_amount")
        + _flt(row, "sgst_amount")
        + _flt(row, "cess_amount")
        + _flt(row, "cess_non_advol_amount")
    )


def _consolidate_lines(rows):
    """Group identical Delivery Note lines, summing quantity, value and weight.

    Keyed on item_code + uom + hs_code + rate so that merging can never change
    the unit price. The key this replaces included qty and amount, so duplicate
    lines had their amounts summed while keeping a single line's quantity.
    """
    consolidated = {}
    ordered_keys = []
    for row in rows:
        key = (
            row.get("item_code"),
            row.get("uom"),
            row.get("gst_hsn_code"),
            _flt(row, "rate"),
        )
        if key not in consolidated:
            consolidated[key] = {
                "description": row.get("item_name") or row.get("item_code"),
                "qty": 0.0,
                "gross": 0.0,
                "weight_kg": 0.0,
            }
            ordered_keys.append(key)
        entry = consolidated[key]
        entry["qty"] += _flt(row, "qty")
        entry["gross"] += _line_gross(row)
        entry["weight_kg"] += _weight_in_kg(
            row.get("total_weight"), row.get("weight_uom")
        )
    return [(key, consolidated[key]) for key in ordered_keys]


def _build_items(rows, pickup_country_code, currency):
    """Build the eShipz parcel `items` array from Delivery Note lines."""
    items = []
    for key, entry in _consolidate_lines(rows):
        item_code, uom, hs_code, _rate = key
        qty = entry["qty"]
        unit_price = flt(entry["gross"] / qty, 2) if qty else 0.0
        unit_weight = entry["weight_kg"]
        if ITEM_WEIGHT_PER_UNIT and qty:
            unit_weight = flt(unit_weight / qty, 3)
        items.append(
            {
                "description": entry["description"],
                "origin_country": pickup_country_code,
                "sku": item_code,
                "hs_code": hs_code,
                "variant": "",
                "quantity": qty,
                "price": {"amount": unit_price, "currency": currency},
                "weight": {"value": unit_weight, "unit": "kg"},
            }
        )
    return items


def _collect_delivery_notes(doc):
    dn_names = [
        row.delivery_note
        for row in (doc.get("shipment_delivery_note") or [])
        if row.delivery_note
    ]
    if not dn_names:
        frappe.throw(
            _("Shipment {0} has no Delivery Note to book against.").format(doc.name)
        )
    return [frappe.get_doc("Delivery Note", name) for name in dn_names]


def _invoice_details(delivery_notes):
    """Resolve the document reference eShipz prints and files against.

    These orders run Sales Order -> Delivery Note -> Shipment and are never
    billed in ERPNext (per_billed 0, status 'To Bill'), so there is no Sales
    Invoice. The Delivery Note is the document travelling with the goods and is
    the only GST reference available.
    """
    invoice_numbers = []
    invoice_dates = []
    gst_invoices = []
    currencies = []

    for dn in delivery_notes:
        invoice_numbers.append(dn.name)
        invoice_dates.append(str(dn.posting_date))
        if dn.currency and dn.currency not in currencies:
            currencies.append(dn.currency)

        ewaybill_number = dn.get("ewaybill") or ""
        ewaybill_date = ""
        if ewaybill_number:
            ewaybill_date = str(
                frappe.db.get_value("e-Waybill Log", ewaybill_number, "created_on") or ""
            )

        gst_invoices.append(
            {
                "invoice_number": dn.name,
                "invoice_date": str(dn.posting_date),
                "invoice_value": flt(dn.grand_total),
                "ewaybill_number": ewaybill_number,
                "ewaybill_date": ewaybill_date,
            }
        )

    if len(currencies) > 1:
        frappe.throw(
            _(
                "This Shipment mixes currencies ({0}). eShipz accepts a single "
                "currency per shipment."
            ).format(", ".join(currencies))
        )

    currency = currencies[0] if currencies else "INR"
    return invoice_numbers, invoice_dates, gst_invoices, currency


def _verify_declared_value(items, rows, delivery_notes, cross_check_grand_total=True):
    """Recompute what eShipz will derive and refuse to book if it disagrees.

    eShipz is sent no authoritative total it is known to honour, so the declared
    value on the label is whatever `sum(quantity * price.amount)` comes to. This
    reproduces that sum and blocks the booking on a mismatch, so a payload defect
    surfaces at booking time instead of on a dispatched label.

    Cross-checked against the Delivery Note grand total rather than the Shipment's
    Value of Goods: the latter is a user-editable mirror of it, so it can drift
    without the goods changing.
    """
    computed = sum(flt(item["quantity"]) * flt(item["price"]["amount"]) for item in items)
    line_total = sum(_line_gross(row) for row in rows)

    if abs(computed - line_total) > DECLARED_VALUE_TOLERANCE:
        frappe.throw(
            _(
                "Declared value check failed: eShipz would derive {0} from the item "
                "lines, but the Delivery Note lines total {1}. Booking stopped so an "
                "incorrect value is not printed on the label."
            ).format(flt(computed, 2), flt(line_total, 2))
        )

    if cross_check_grand_total:
        grand_total = sum(flt(dn.grand_total) for dn in delivery_notes)
        if grand_total and abs(line_total - grand_total) > DECLARED_VALUE_TOLERANCE:
            frappe.throw(
                _(
                    "Declared value check failed: the Delivery Note lines total {0} but "
                    "the Delivery Note grand total is {1}. This usually means a discount "
                    "or charge applies to the document as a whole rather than to its "
                    "lines. Booking stopped so an incorrect value is not printed on the "
                    "label."
                ).format(flt(line_total, 2), flt(grand_total, 2))
            )

    return flt(computed, 2)


def _build_parcels(doc, delivery_notes, item_data, pickup_country_code, currency):
    """Build the eShipz `parcels` array. Returns (parcels, declared_value)."""
    parcel_rows = doc.get("shipment_parcel") or []
    if not parcel_rows:
        frappe.throw(_("Please enter Shipment Parcel information"))
    if len(parcel_rows) > 1:
        frappe.throw(
            _(
                "eShipz supports one parcel per shipment, but this Shipment has {0}. "
                "Each parcel would be sent the full item list, declaring the order "
                "value once per parcel. Use the parcel Count field for multiple boxes."
            ).format(len(parcel_rows))
        )

    parcel = parcel_rows[0]
    if item_data:
        rows = item_data.get(str(parcel.idx)) or []
    else:
        rows = [row for dn in delivery_notes for row in dn.items]

    items = _build_items(rows, pickup_country_code, currency)
    declared_value = _verify_declared_value(
        items, rows, delivery_notes, cross_check_grand_total=not item_data
    )

    parcels = [
        {
            "description": doc.description_of_content,
            "box_type": doc.shipment_type,
            "quantity": parcel.count,
            "weight": {"value": parcel.weight, "unit": "kg"},
            "dimension": {
                "width": parcel.width,
                "height": parcel.height,
                "length": parcel.length,
                "unit": "cm",
            },
            "items": items,
            "order_value": declared_value,
        }
    ]
    return parcels, declared_value


def _get_shopify_order_number(doc):
    """
    Resolve the linked Sales Order's shopify_order_number via the Shipment's
    Delivery Note -> Delivery Note Item.against_sales_order. Always exactly one
    Delivery Note / one Sales Order per Shipment in practice (both Shipment-
    creation call sites in bombaysweets_customization/api.py append a single
    shipment_delivery_note row), so the first match is used.
    """
    dn_names = [r.delivery_note for r in (doc.get("shipment_delivery_note") or []) if r.delivery_note]
    if not dn_names:
        return None

    so_names = frappe.get_all(
        "Delivery Note Item",
        filters={"parent": ["in", dn_names], "against_sales_order": ["is", "set"]},
        pluck="against_sales_order",
    )
    if not so_names:
        return None

    return frappe.db.get_value("Sales Order", so_names[0], "shopify_order_number")


@frappe.whitelist()
def fetch_available_services(docname: str):
    doc = frappe.get_doc('Shipment', docname)
    
    pickup_address = frappe.get_doc('Address', doc.pickup_address_name)
    delivery_address = frappe.get_doc('Address', doc.delivery_address_name)

    def get_country_code(country_name):
        country = frappe.get_doc('Country', country_name)
        return country.code.upper()

    pickup_country_code = get_country_code(pickup_address.country)
    delivery_country_code = get_country_code(delivery_address.country)

    api_token = frappe.db.get_single_value('eShipz Settings', 'api_token')
    if not api_token:
        frappe.throw(_("API token not found in eShipz Settings"))

    url = "https://app.eshipz.com/api/v2/services"
    headers = {
        "X-API-TOKEN": api_token,
        "Content-Type": "application/json"
    }

    data = {    
        "is_document": False,
        "shipment": {
            "is_reverse": False,
            "purpose": doc.fsl_purpose,
            "is_cod": False,
            "collect_on_delivery": {"amount": 0, "currency": "INR"},
            "ship_from": {
                "contact_name": doc.pickup_contact_person,
                "company_name": doc.pickup_company,
                "street1": pickup_address.address_line1,
                "city": pickup_address.city,
                "state": pickup_address.state,
                "postal_code": pickup_address.pincode,
                "country": pickup_country_code,
                "type": doc.fsl_pickup_type,
                "phone": pickup_address.phone,
                "email": pickup_address.email_id,
                "is_primary": True
            },
            "ship_to": {
                "contact_name": delivery_address.get("custom_recipient_name") or doc.delivery_contact_name,
                "company_name": delivery_address.address_title,
                "street1": delivery_address.address_line1,
                "city": delivery_address.city,
                "state": delivery_address.state,
                "postal_code": delivery_address.pincode,
                "country": delivery_country_code,
                "type": doc.fsl_delivery_type,
                "phone": delivery_address.get("custom_recipient_phone") or delivery_address.phone,
                "email": delivery_address.email_id,
            },
            "return_to": {
                "contact_name": doc.pickup_contact_person,
                "company_name": doc.pickup_company,
                "street1": pickup_address.address_line1,
                "city": pickup_address.city,
                "state": pickup_address.state,
                "postal_code": pickup_address.pincode,
                "country": pickup_country_code,
                "type": doc.fsl_pickup_type,
                "phone": pickup_address.phone,
                "email": pickup_address.email_id,
                "is_primary": True
            },
            "parcels": [
                {
                    "description": doc.description_of_content,
                    "box_type": doc.shipment_type,
                    "weight": {"value": parcel.weight, "unit": "kg"},
                    "dimension": {
                        "width": parcel.width,
                        "height": parcel.height,
                        "length": parcel.length,
                        "unit": "cm"
                    },
                    "items": [
                        {
                            "description": doc.description_of_content,
                            "origin_country": pickup_country_code,
                            "quantity": parcel.count,
                            # Unit price: eShipz multiplies by `quantity`, so sending
                            # the full value_of_goods here overstated every quote with
                            # a parcel count above 1 and skewed service selection.
                            "price": {
                                "amount": flt(doc.value_of_goods) / parcel.count
                                if parcel.count
                                else flt(doc.value_of_goods),
                                "currency": "INR"
                            },
                            "weight": {
                                "unit": "kg",
                                "value": parcel.weight
                            }
                        }
                    ]
                } for parcel in doc.get("shipment_parcel")
            ]
        }
    }

    json_data = json.dumps(data, separators=(',', ':'), default=lambda x: str(x).lower() if isinstance(x, bool) else x)

    result_log = send_logged_request(
        method="POST", url=url, request_description="Fetch Available Services",
        reference_doctype="Shipment", reference_docname=docname,
        headers=headers, data=json_data,
    )
    response = result_log.response
    if response is None:
        frappe.throw(_(
            "Failed to fetch services — could not reach carrier. "
            "See Integration Request {0} for details."
        ).format(result_log.log_name))

    if response.status_code == 200:
        result = response.json()
        if 'rates' in result['data']:
            rates_list = result['data']['rates']
            if rates_list:
                return [rate for rate in rates_list if rate.get('code') in [200, 201]]

            frappe.throw(_(
                "Failed to fetch services: {0}. See Integration Request {1}."
            ).format(response.text, result_log.log_name))
        else:
            frappe.throw(_(
                "Rates key not found in API response: {0}. See Integration Request {1}."
            ).format(frappe.as_json(result), result_log.log_name))
    else:
        frappe.throw(_(
            "Failed to fetch services: {0}. See Integration Request {1}."
        ).format(response.text, result_log.log_name))

@frappe.whitelist()
def create_shipment(docname: str, selected_service: str, item_data: str | None = None):
    doc = frappe.get_doc('Shipment', docname)
    
    selected_service = json.loads(selected_service)
    if item_data:
        item_data = json.loads(item_data)

    pickup_address = frappe.get_doc('Address', doc.pickup_address_name)
    delivery_address = frappe.get_doc('Address', doc.delivery_address_name)
    
    def get_country_code(country_name):
        country = frappe.get_doc('Country', country_name)
        return country.code.upper()

    pickup_country_code = get_country_code(pickup_address.country)
    delivery_country_code = get_country_code(delivery_address.country)

    api_token = frappe.db.get_single_value('eShipz Settings', 'api_token')
    if not api_token:
        frappe.throw(_("API token not found in eShipz Settings"))

    url = "https://app.eshipz.com/api/v1/create-shipments"
    headers = {
        "X-API-TOKEN": api_token,
        "Content-Type": "application/json"
    }

    charged_weight = sum(parcel.weight for parcel in doc.get("shipment_parcel"))

    delivery_notes = _collect_delivery_notes(doc)
    invoice_numbers, invoice_dates, gst_invoices, invoice_currency = _invoice_details(
        delivery_notes
    )
    # Declared value is carried inside the parcel as `order_value`; eShipz has no
    # shipment-level total field, so nothing else consumes it here.
    parcels, _declared_value = _build_parcels(
        doc, delivery_notes, item_data, pickup_country_code, invoice_currency
    )

    data = {
        "billing": {
            "paid_by": "shipper"
        },
        "vendor_id": selected_service['vendor_id'],
        "description": selected_service['description'],
        "slug": selected_service['slug'],
        "purpose": doc.fsl_purpose,
        "order_source": "erpnext",
        "parcel_contents": doc.description_of_content,
        "is_document": False,
        "service_type": selected_service['selected_service_type'],
        "charged_weight": {
            "unit": "KG",
            "value": charged_weight
        },
        "customer_reference": _get_shopify_order_number(doc) or doc.name,
        "invoice_number": ", ".join(invoice_numbers),
        "invoice_date": ", ".join(invoice_dates),
        "is_cod": False,
        "collect_on_delivery": {"amount": 0, "currency": invoice_currency},
        "shipment": {
            "ship_from": {
                "contact_name": doc.pickup_contact_person,
                "company_name": doc.pickup_company,
                "street1": pickup_address.address_line1,
                "street2": pickup_address.address_line2,
                "city": pickup_address.city,
                "state": pickup_address.state,
                "postal_code": pickup_address.pincode,
                "phone": pickup_address.phone,
                "email": pickup_address.email_id,
                "tax_id": pickup_address.gstin,
                "country": pickup_country_code,
                "type": doc.fsl_pickup_type
            },
            "ship_to": {
                "contact_name": delivery_address.get("custom_recipient_name") or doc.delivery_contact_name,
                "company_name": delivery_address.address_title,
                "street1": delivery_address.address_line1,
                "street2": delivery_address.address_line2,
                "city": delivery_address.city,
                "state": delivery_address.state,
                "postal_code": delivery_address.pincode,
                "phone": delivery_address.get("custom_recipient_phone") or delivery_address.phone,
                "email": delivery_address.email_id,
                "country": delivery_country_code,
                "type": doc.fsl_delivery_type
            },
            "return_to": {
                "contact_name": doc.pickup_contact_person,
                "company_name": doc.pickup_company,
                "street1": pickup_address.address_line1,
                "street2": pickup_address.address_line2,
                "city": pickup_address.city,
                "state": pickup_address.state,
                "postal_code": pickup_address.pincode,
                "phone": pickup_address.phone,
                "email": pickup_address.email_id,
                "tax_id": pickup_address.gstin,
                "country": pickup_country_code,
                "type": doc.fsl_pickup_type
            },
            "is_reverse": False,
            "is_to_pay": False,
            "parcels": parcels
        },
        "gst_invoices": gst_invoices
    }

    json_data = json.dumps(data, separators=(',', ':'), default=lambda x: str(x).lower() if isinstance(x, bool) else x)

    result_log = send_logged_request(
        method="POST", url=url, request_description="Create Shipment",
        reference_doctype="Shipment", reference_docname=docname,
        headers=headers, data=json_data,
    )
    response = result_log.response
    if response is None:
        frappe.throw(_(
            "Failed to create shipment — could not reach carrier. "
            "See Integration Request {0} for details."
        ).format(result_log.log_name))

    if response.status_code != 200:
        frappe.throw(_(
            "Failed to create shipment: {0}. See Integration Request {1}."
        ).format(response.text, result_log.log_name))

    result = response.json()
    try:
        if 'files' not in result['data']:
            raise KeyError('data.files')
        label_url = result['data']['files']['label']['label_meta']['url']
        awb_number = result['data']['files']['label']['label_meta']['awb']
        service_provider = result['data']['slug']
        tracking_status_info = result['data']['status']
        carrier_service = result['data']['service_type']
        shipment_id = result['data']['order_id']
    except (KeyError, TypeError, ValueError) as exc:
        record_log_error(result_log.log_name, f"Unexpected response shape from carrier: {exc}")
        frappe.throw(_(
            "Shipment booking response could not be parsed — the carrier's reply was in an "
            "unexpected format. The booking may have succeeded on the carrier's side even "
            "though this could not be confirmed automatically. See Integration Request {0} "
            "for the full raw response before re-attempting booking."
        ).format(result_log.log_name))

    doc.db_set('tracking_url', label_url)
    doc.db_set('awb_number', awb_number)
    doc.db_set('status', "Booked")
    doc.db_set('tracking_status', "In Progress")
    doc.db_set('service_provider', service_provider)
    doc.db_set('shipment_id', shipment_id)
    doc.db_set('tracking_status_info', tracking_status_info)
    doc.db_set('carrier_service', carrier_service)
    return {
        "label_url": label_url,
        "awb_number": awb_number,
        "service_provider": service_provider,
        "tracking_status_info": tracking_status_info,
        "carrier_service": carrier_service,
        "shipment_id": shipment_id,
    }

@frappe.whitelist()
def create_rule_based_shipment(docname: str, item_data: str | None = None):
    doc = frappe.get_doc('Shipment', docname)
    
    if item_data:
        item_data = json.loads(item_data)

    pickup_address = frappe.get_doc('Address', doc.pickup_address_name)
    delivery_address = frappe.get_doc('Address', doc.delivery_address_name)
    
    def get_country_code(country_name):
        country = frappe.get_doc('Country', country_name)
        return country.code.upper()

    pickup_country_code = get_country_code(pickup_address.country)
    delivery_country_code = get_country_code(delivery_address.country)

    api_token = frappe.db.get_single_value('eShipz Settings', 'api_token')
    if not api_token:
        frappe.throw(_("API token not found in eShipz Settings"))

    url = "https://app.eshipz.com/api/v1/create-shipments/rule-based"
    headers = {
        "X-API-TOKEN": api_token,
        "Content-Type": "application/json"
    }

    charged_weight = sum(parcel.weight for parcel in doc.get("shipment_parcel"))

    delivery_notes = _collect_delivery_notes(doc)
    invoice_numbers, invoice_dates, gst_invoices, invoice_currency = _invoice_details(
        delivery_notes
    )
    # Declared value is carried inside the parcel as `order_value`; eShipz has no
    # shipment-level total field, so nothing else consumes it here.
    parcels, _declared_value = _build_parcels(
        doc, delivery_notes, item_data, pickup_country_code, invoice_currency
    )

    data = {
        "billing": {
            "paid_by": "shipper"
        },
        "vendor_id": None,
        "description": "Bluedart",
        "slug": None,
        "purpose": doc.fsl_purpose,
        "order_source": "erpnext",
        "parcel_contents": doc.description_of_content,
        "is_document": False,
        "service_type": None,
        "charged_weight": {
            "unit": "KG",
            "value": charged_weight
        },
        "customer_reference": _get_shopify_order_number(doc) or doc.name,
        "invoice_number": ", ".join(invoice_numbers),
        "invoice_date": ", ".join(invoice_dates),
        "is_cod": False,
        "collect_on_delivery": {"amount": 0, "currency": invoice_currency},
        "shipment": {
            "ship_from": {
                "contact_name": doc.pickup_contact_person,
                "company_name": doc.pickup_company,
                "street1": pickup_address.address_line1,
                "street2": pickup_address.address_line2,
                "city": pickup_address.city,
                "state": pickup_address.state,
                "postal_code": pickup_address.pincode,
                "phone": pickup_address.phone,
                "email": pickup_address.email_id,
                "tax_id": pickup_address.gstin,
                "country": pickup_country_code,
                "type": doc.fsl_pickup_type
            },
            "ship_to": {
                "contact_name": delivery_address.get("custom_recipient_name") or doc.delivery_contact_name,
                "company_name": delivery_address.address_title,
                "street1": delivery_address.address_line1,
                "street2": delivery_address.address_line2,
                "city": delivery_address.city,
                "state": delivery_address.state,
                "postal_code": delivery_address.pincode,
                "phone": delivery_address.get("custom_recipient_phone") or delivery_address.phone,
                "email": delivery_address.email_id,
                "country": delivery_country_code,
                "type": doc.fsl_delivery_type
            },
            "return_to": {
                "contact_name": doc.pickup_contact_person,
                "company_name": doc.pickup_company,
                "street1": pickup_address.address_line1,
                "street2": pickup_address.address_line2,
                "city": pickup_address.city,
                "state": pickup_address.state,
                "postal_code": pickup_address.pincode,
                "phone": pickup_address.phone,
                "email": pickup_address.email_id,
                "tax_id": pickup_address.gstin,
                "country": pickup_country_code,
                "type": doc.fsl_pickup_type
            },
            "is_reverse": False,
            "is_to_pay": False,
            "parcels": parcels
        },
        "gst_invoices": gst_invoices
    }

    json_data = json.dumps(data, separators=(',', ':'), default=lambda x: str(x).lower() if isinstance(x, bool) else x)

    result_log = send_logged_request(
        method="POST", url=url, request_description="Create Rule-Based Shipment",
        reference_doctype="Shipment", reference_docname=docname,
        headers=headers, data=json_data,
    )
    response = result_log.response
    if response is None:
        frappe.throw(_(
            "Failed to create shipment — could not reach carrier. "
            "See Integration Request {0} for details."
        ).format(result_log.log_name))

    if response.status_code != 200:
        frappe.throw(_(
            "Failed to create shipment: {0}. See Integration Request {1}."
        ).format(response.text, result_log.log_name))

    result = response.json()
    try:
        if 'files' not in result['data']:
            raise KeyError('data.files')
        label_url = result['data']['files']['label']['label_meta']['url']
        awb_number = result['data']['files']['label']['label_meta']['awb']
        service_provider = result['data']['slug']
        tracking_status_info = result['data']['status']
        carrier_service = result['data']['service_type']
        shipment_id = result['data']['order_id']
    except (KeyError, TypeError, ValueError) as exc:
        record_log_error(result_log.log_name, f"Unexpected response shape from carrier: {exc}")
        frappe.throw(_(
            "Shipment booking response could not be parsed — the carrier's reply was in an "
            "unexpected format. The booking may have succeeded on the carrier's side even "
            "though this could not be confirmed automatically. See Integration Request {0} "
            "for the full raw response before re-attempting booking."
        ).format(result_log.log_name))

    doc.db_set('tracking_url', label_url)
    doc.db_set('awb_number', awb_number)
    doc.db_set('status', "Booked")
    doc.db_set('tracking_status', "In Progress")
    doc.db_set('service_provider', service_provider)
    doc.db_set('shipment_id', shipment_id)
    doc.db_set('tracking_status_info', tracking_status_info)
    doc.db_set('carrier_service', carrier_service)
    return {
        "label_url": label_url,
        "awb_number": awb_number,
        "service_provider": service_provider,
        "tracking_status_info": tracking_status_info,
        "carrier_service": carrier_service,
        "shipment_id": shipment_id,
    }

@frappe.whitelist()
def cancel_shipment(docname: str):
    doc = frappe.get_doc('Shipment', docname)

    api_token = frappe.db.get_single_value('eShipz Settings', 'api_token')
    if not api_token:
        frappe.throw(_("API token not found in eShipz Settings"))

    url = "https://app.eshipz.com/api/v1/cancel"
    headers = {
        "X-API-TOKEN": api_token,
        "Content-Type": "application/json"
    }

    data = {
        "order_id" :[
            doc.shipment_id,
            ]
    }

    result_log = send_logged_request(
        method="POST", url=url, request_description="Cancel Shipment",
        reference_doctype="Shipment", reference_docname=docname,
        headers=headers, json_body=data,
    )
    response = result_log.response
    if response is None:
        frappe.throw(_(
            "Failed to cancel shipment — could not reach carrier. "
            "See Integration Request {0} for details."
        ).format(result_log.log_name))

    if response.status_code == 200:
        doc.db_set('tracking_url', "")
        doc.db_set('status', "Cancelled")
        doc.db_set('tracking_status', "")
        doc.db_set('service_provider', "")
        doc.db_set('tracking_status_info', "Cancelled")
        doc.db_set('carrier_service', "")

        # Clear SO reference before cancelling the doc so the link is gone before docstatus flips
        _clear_shipment_references(docname)

        try:
            fresh = frappe.get_doc("Shipment", docname)
            if fresh.docstatus == 1:
                fresh.cancel()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "eShipz: ERPNext Shipment cancel failed")
    else:
        frappe.throw(_(
            "Failed to cancel shipment: {0}. See Integration Request {1}."
        ).format(response.text, result_log.log_name))


def _clear_shipment_references(shipment_name: str) -> None:
    """Clear custom_shipment_reference on every Sales Order that references this shipment.
    Single query — no N+1."""
    frappe.db.set_value(
        "Sales Order",
        {"custom_shipment_reference": shipment_name},
        "custom_shipment_reference",
        None,
    )

@frappe.whitelist()
def update_status(docname: str):

    doc = frappe.get_doc('Shipment', docname)

    api_token = frappe.db.get_single_value('eShipz Settings', 'api_token')
    if not api_token:
        frappe.throw(_("API token not found in eShipz Settings"))

    url = "https://app.eshipz.com/api/v2/trackings"
    headers = {
        "X-API-TOKEN": api_token,
        "Content-Type": "application/json"
    }

    data = {
        "track_id": doc.awb_number
    }

    result_log = send_logged_request(
        method="POST", url=url, request_description="Update Tracking Status",
        reference_doctype="Shipment", reference_docname=docname,
        headers=headers, json_body=data,
    )
    response = result_log.response
    if response is None:
        frappe.throw(_(
            "Failed to retrieve shipment status — could not reach carrier. "
            "See Integration Request {0} for details."
        ).format(result_log.log_name))

    if response.status_code == 200:
        result = response.json()
        if not result:
            frappe.throw(_(
                "API response is empty. See Integration Request {0}."
            ).format(result_log.log_name))

        if not isinstance(result, list):
            frappe.throw(_(
                "API response format is not a list: {0}. See Integration Request {1}."
            ).format(frappe.as_json(result), result_log.log_name))

        tracking_data = result[0] if result else None
        if not tracking_data or 'checkpoints' not in tracking_data:
            frappe.throw(_(
                "Invalid tracking data format: {0}. See Integration Request {1}."
            ).format(frappe.as_json(result), result_log.log_name))

        checkpoints = tracking_data.get('checkpoints', [])
        delivery_date = tracking_data.get('delivery_date')
        expected_delivery_date = tracking_data.get('expected_delivery_date')
        shipment_status = tracking_data.get('shipment_status')
        tag = tracking_data.get('tag')

        latest_city = None
        latest_remark = None
        latest_tag = None

        if checkpoints:
            latest_checkpoint = sorted(checkpoints, key=lambda x: datetime.strptime(x['date'], "%a, %d %b %Y %H:%M:%S %Z"), reverse=True)[0]
            latest_city = latest_checkpoint.get('city')
            latest_remark = latest_checkpoint.get('remark')
            latest_tag = latest_checkpoint.get('tag')

            doc.db_set('fsl_latest_location', latest_city)

        new_tracking_status = None
        if tag == "Delivered":
            doc.db_set('status', "Completed")
            doc.db_set('tracking_status', "Delivered")
            new_tracking_status = "Delivered"
        elif tag == "InTransit":
            doc.db_set('tracking_status', "In Progress")
            new_tracking_status = "In Progress"

        if new_tracking_status:
            try:
                from bombaysweets_customization.bombaysweets_customization.api import (
                    sync_shipment_tracking_status_to_so,
                )

                sync_shipment_tracking_status_to_so(doc.name, new_tracking_status)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(), f"eShipz update_status: SO tracking status sync failed for {doc.name}"
                )

        if delivery_date:
            delivery_date_erp = datetime.strptime(delivery_date, "%a, %d %b %Y %H:%M:%S %Z").strftime("%Y-%m-%d %H:%M:%S")
            doc.db_set('fsl_delivery_date', delivery_date_erp)

        if expected_delivery_date:
            expected_delivery_date_erp = datetime.strptime(expected_delivery_date, "%a, %d %b %Y %H:%M:%S %Z").strftime("%Y-%m-%d %H:%M:%S")
            doc.db_set('fsl_expected_delivery_date', expected_delivery_date_erp)

        last_update_received = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        doc.db_set('fsl_last_update_received', last_update_received)
        doc.db_set('tracking_status_info', latest_remark)

        return {
            "latest_checkpoint": {
                "fsl_latest_location": latest_city,
                "remark": latest_remark,
                "tag": latest_tag
            },
            "tracking_status_info": latest_remark,
            "fsl_delivery_date": delivery_date_erp if delivery_date else None,
            "fsl_expected_delivery_date": expected_delivery_date_erp if expected_delivery_date else None,
            "shipment_status": shipment_status,
            "tag": tag,
        }
    else:
        frappe.throw(_(
            "Failed to retrieve shipment status: {0}. See Integration Request {1}."
        ).format(response.text, result_log.log_name))

@frappe.whitelist()
def get_delivery_note_items(delivery_note: str):
    if not frappe.has_permission('Delivery Note', 'read', delivery_note):
        raise frappe.PermissionError
    
    # Field list mirrors what _build_items()/_line_gross() read, so a client-supplied
    # item_data payload carries the same per-line tax and weight the server path uses.
    items = frappe.get_all('Delivery Note Item',
        filters={'parent': delivery_note},
        fields=[
            'name', 'item_code', 'item_name', 'qty', 'uom', 'gst_hsn_code',
            'rate', 'amount', 'net_amount',
            'igst_amount', 'cgst_amount', 'sgst_amount',
            'cess_amount', 'cess_non_advol_amount',
            'total_weight', 'weight_uom',
        ],
        parent='Delivery Note',
    )
    return items