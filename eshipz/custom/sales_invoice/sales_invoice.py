
import frappe
from frappe.model.document import Document


@frappe.whitelist()
def update_delivery_note(doc: Document, method: str | None = None):
    # Group SI items by delivery note to avoid N+1 saves
    dn_item_codes: dict[str, set] = {}
    for item in doc.items:
        if item.delivery_note:
            dn_item_codes.setdefault(item.delivery_note, set()).add(item.item_code)

    if not dn_item_codes:
        return

    # Only touch Delivery Notes that are actually part of an eShipz-booked
    # Shipment — service_provider is set (and only cleared on cancel) by
    # book_shipment on a successful booking, see custom/shipment/shipment.py.
    # This hook exists purely to backfill against_sales_invoice for eShipz's
    # own tracking/label needs, so it must be a no-op for every other
    # invoice (e.g. a walk-in/B2B invoice with no courier booking at all) —
    # previously it ran unconditionally on every Sales Invoice submission.
    dn_names = list(dn_item_codes.keys())
    booked_shipment_names = frappe.get_all(
        "Shipment",
        filters={"docstatus": ["!=", 2], "service_provider": ["not in", ["", None]]},
        pluck="name",
    )
    shipped_dn_names = set()
    if booked_shipment_names:
        shipped_dn_names = set(frappe.get_all(
            "Shipment Delivery Note",
            filters={"delivery_note": ["in", dn_names], "parent": ["in", booked_shipment_names]},
            pluck="delivery_note",
        ))

    for dn_name, item_codes in dn_item_codes.items():
        if dn_name not in shipped_dn_names:
            continue
        delivery_note = frappe.get_doc("Delivery Note", dn_name)
        for dn_item in delivery_note.items:
            if dn_item.item_code in item_codes:
                # db_set bypasses the update-after-submit lock safely — a
                # plain field assignment + delivery_note.save() here
                # previously crashed on every submitted DN (regardless of
                # eShipz involvement) with UpdateAfterSubmitError, since
                # against_sales_invoice has no allow_on_submit.
                dn_item.db_set("against_sales_invoice", doc.name, update_modified=False)
