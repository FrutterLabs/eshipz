// apps/your_custom_app/your_custom_app/public/js/shipment_list.js

frappe.listview_settings['Shipment'] = {
    onload: function (listview) {

        listview.page.add_button(__('Create & Print Label'), function () {

            let selected_items = listview.get_checked_items();

            if (selected_items.length === 0) {
                frappe.msgprint(__('Please select at least one shipment.'));
                return;
            }

            if (selected_items.length > 1) {
                frappe.show_alert({
                    message: __('Please select only one shipment at a time.'),
                    indicator: 'orange'
                });
                return;
            }

            selected_items.forEach(doc => {
                if (doc.docstatus === 1 && !doc.awb_number) {
                    process_list_shipment(doc.name);
                } else if (doc.docstatus === 0) {
                    frappe.show_alert({ message: `Skipping ${doc.name} - It is still a Draft. Please submit it first.`, indicator: 'orange' });
                } else if (doc.awb_number) {
                    frappe.show_alert({ message: `Skipping ${doc.name} - AWB already generated.`, indicator: 'blue' });
                }
            });

            setTimeout(() => listview.refresh(), 2000);
        });
    }

};

function process_list_shipment(docname) {
    frappe.show_alert({ message: `Finding Bluedart Service for ${docname}...`, indicator: 'blue' });

    frappe.call({
        method: 'eshipz.custom.shipment.shipment.fetch_available_services',
        args: { docname: docname },
        callback: function (r) {
            if (r.message && r.message.length > 0) {

                let bluedart_service = null;
                let selected_tech = null;

                for (let s of r.message) {
                    if (s.slug && s.slug.toLowerCase().includes('bluedart')) {
                        bluedart_service = s;
                        if (s.technicality && s.technicality.length > 0) {

                            // CHANGED: Match the exact API string format for "eTailPrePaidAir"
                            let etail_service = s.technicality.find(t =>
                                t.service_type && t.service_type.toLowerCase().includes('etailprepaid')
                            );

                            if (etail_service) {
                                selected_tech = etail_service.service_type;
                            } else {
                                // Fallback to the first available service if E-tail isn't available for this route
                                selected_tech = s.technicality[0].service_type;
                            }
                        }
                        break;
                    }
                }

                if (!bluedart_service) {
                    frappe.msgprint({ title: __('Error'), indicator: 'red', message: `Bluedart is not available for ${docname}.` });
                    return;
                }

                bluedart_service.selected_service_type = selected_tech;

                frappe.show_alert({ message: `Creating Shipment ${docname}...`, indicator: 'blue' });

                frappe.call({
                    method: 'eshipz.custom.shipment.shipment.create_shipment',
                    args: {
                        docname: docname,
                        selected_service: JSON.stringify(bluedart_service),
                        item_data: ""
                    },
                    callback: function (res) {
                        if (res.message && res.message.label_url) {
                            frappe.show_alert({ message: `Success: ${docname} Created!`, indicator: 'green' });
                            window.open(res.message.label_url, '_blank');
                        } else {
                            frappe.msgprint({ title: __('Error'), indicator: 'red', message: `Failed to retrieve the Label URL for ${docname}.` });
                        }
                    }
                });

            } else {
                frappe.msgprint({ title: __('Error'), indicator: 'red', message: `No courier services returned for ${docname}.` });
            }
        }
    });
}