// Copyright (c) 2024, Frutter Software Labs Private Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on('Shipment', {
	refresh: function (frm) {
		const assigned = (frm.doc.custom_assigned_courier || '').toLowerCase();
		if (assigned && !assigned.includes('eshipz')) return;

		frappe.call({
			method: 'frappe.client.get_value',
			args: { doctype: 'eShipz Settings', fieldname: 'enabled' },
			callback: function (r) {
				if (r.message && r.message.enabled == 1) {
					_render_eshipz_buttons(frm);
				}
			}
		});
	}
});

function _style_group_item($btn, bg_color) {
	if (!$btn || !$btn.length) return;
	$btn.css({ background: bg_color, color: '#fff', 'font-weight': '500' });
	$btn.parent().css({ 'padding-top': '2px', 'padding-bottom': '2px' });
}

function _render_eshipz_buttons(frm) {
	const GROUP = __('eShipz');

	// ==========================================
	// 1-CLICK CREATE & PRINT LABEL BUTTON
	// ==========================================
	if (frm.doc.docstatus == 1 && !frm.doc.awb_number) {
		frm.add_custom_button(__('Create Shipment & Print Label'), function () {

			frappe.call({
				method: 'eshipz.custom.shipment.shipment.fetch_available_services',
				args: { docname: frm.docname },
				freeze: true,
				freeze_message: __('Finding Bluedart Service... ⏳☕'),
				callback: function (r) {
					if (r.message && r.message.length > 0) {

						let bluedart_service = null;
						let selected_tech = null;

						for (let s of r.message) {
							if (s.slug && s.slug.toLowerCase().includes('bluedart')) {
								bluedart_service = s;
								if (s.technicality && s.technicality.length > 0) {
									let etail_service = s.technicality.find(t =>
										t.service_type && t.service_type.toLowerCase().includes('etailprepaid')
									);
									selected_tech = etail_service
										? etail_service.service_type
										: s.technicality[0].service_type;
								}
								break;
							}
						}

						if (!bluedart_service) {
							frappe.msgprint({ title: __('Error'), indicator: 'red', message: __('Bluedart is not available for this specific route.') });
							return;
						}

						bluedart_service.selected_service_type = selected_tech;

						frappe.call({
							method: 'eshipz.custom.shipment.shipment.create_shipment',
							args: {
								docname: frm.docname,
								selected_service: JSON.stringify(bluedart_service),
								item_data: ''
							},
							freeze: true,
							freeze_message: __('Creating Shipment with eShipz... ⏳☕'),
							callback: function (res) {
								if (res.message && res.message.label_url) {
									frappe.show_alert({ message: __('Shipment Created! Opening Label...'), indicator: 'green' });
									window.open(res.message.label_url, '_blank');
									frm.reload_doc();
								} else {
									frappe.msgprint({ title: __('Error'), indicator: 'red', message: __('Failed to retrieve the Label URL from the API.') });
								}
							}
						});

					} else {
						frappe.msgprint({ title: __('Error'), indicator: 'red', message: __('No courier services returned from eShipz.') });
					}
				}
			});
		}, GROUP);
	}

	// ==========================================
	// POST-CREATION MANAGEMENT BUTTONS
	// ==========================================
	if (frm.doc.docstatus == 1 && frm.doc.awb_number && frm.doc.status != 'Cancelled') {

		frm.add_custom_button(__('Download/Print Label'), function () {
			window.open(frm.doc.tracking_url, '_blank');
		}, GROUP);

		frm.add_custom_button(__('Cancel Shipment'), function () {
			frappe.call({
				method: 'eshipz.custom.shipment.shipment.cancel_shipment',
				args: { docname: frm.docname },
				freeze: true,
				freeze_message: __('Cancelling Shipment... Please wait...⏳☕'),
				callback: function (res) {
					if (res.message !== undefined) {
						frappe.show_alert({ message: __('Shipment Cancelled'), indicator: 'orange' });
						frm.reload_doc();
					}
				}
			});
		}, GROUP);

		frm.add_custom_button(__('Track Shipment'), function () {
			var track_url = `https://track.eshipz.com/track?awb=${frm.doc.awb_number}&slug=${frm.doc.service_provider}`;
			window.open(track_url, '_blank');
		}, GROUP);

		frm.add_custom_button(__('Update Status'), function () {
			frappe.call({
				method: 'eshipz.custom.shipment.shipment.update_status',
				args: { docname: frm.docname },
				freeze: true,
				freeze_message: __('Getting Status... Please wait...⏳☕'),
				callback: function (res) {
					if (res.message) {
						frappe.show_alert({ message: __('Status Updated'), indicator: 'green' });
						frm.reload_doc();
					}
				}
			});
		}, GROUP);
	}

	// Apply colors after DOM is ready
	setTimeout(function () {
		_style_group_item(frm.custom_buttons[__('Create Shipment & Print Label')], '#239b56');
		_style_group_item(frm.custom_buttons[__('Download/Print Label')], '#21618c');
		_style_group_item(frm.custom_buttons[__('Cancel Shipment')], '#c0392b');
		_style_group_item(frm.custom_buttons[__('Track Shipment')], '#196f3d');
		_style_group_item(frm.custom_buttons[__('Update Status')], '#1a9870');

		if (frm.custom_buttons[GROUP]) {
			frm.custom_buttons[GROUP]
				.removeClass('btn-default btn-secondary')
				.addClass('btn-primary')
				.css({ background: '#1a5276', color: 'white', 'border-color': '#1a5276' });
		}
	}, 0);
}
