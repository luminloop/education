import frappe
from frappe import _
from frappe.utils import validate_phone_number, cint, nowdate
from payments.utils import get_payment_gateway_controller, create_payment_gateway
from erpnext.accounts.doctype.journal_entry.journal_entry import (
	get_payment_entry_against_invoice,
)
from erpnext.accounts.doctype.payment_entry.test_payment_entry import get_payment_entry


def get_details(docname):
	details = frappe.db.get_value(
		"Sales Invoice", docname, ["name", "currency", "outstanding_amount"], as_dict=1
	)
	return details


def get_payment_gateway():
	"""Get the configured payment gateway from Education Settings"""
	settings = frappe.get_single("Education Settings")
	
	# Check if a specific payment gateway is configured
	if hasattr(settings, 'payment_gateway') and settings.payment_gateway:
		return settings.payment_gateway
	
	# Fallback to razorpay for backward compatibility
	return "Razorpay"


def get_gateway_client(gateway_name=None):
	"""Get payment gateway client"""
	if not gateway_name:
		gateway_name = get_payment_gateway()
	
	# Create payment gateway if it doesn't exist
	if not frappe.db.exists("Payment Gateway", gateway_name):
		create_payment_gateway(gateway_name)
	
	return get_payment_gateway_controller(gateway_name)


def create_order(gateway_name, amount, currency):
	"""Create payment order using the specified gateway"""
	try:
		client = get_gateway_client(gateway_name)
		return client.create_order({
			"amount": cint(amount) * 100,
			"currency": currency,
		})
	except Exception as e:
		frappe.throw(
			_(
				"Error during payment: {0} Please contact the Administrator. Amount {1} Currency {2} Formatted {3}"
			).format(e, amount, currency, cint(amount))
		)


@frappe.whitelist()
def get_payment_options(doctype, docname, phone, currency=None):
	if not frappe.db.exists(doctype, docname):
		frappe.throw(_("Invalid document provided."))
	validate_phone_number(phone_number=phone, throw=True)
	details = get_details(docname)
	
	gateway_name = get_payment_gateway()
	client = get_gateway_client(gateway_name)
	order = create_order(gateway_name, details.outstanding_amount, details.currency)
	
	# Get gateway-specific payment options
	options = {
		"amount": cint(order["amount"]) * 100,
		"currency": order["currency"],
		"order_id": order["id"],
	}
	
	# Add gateway-specific fields
	if hasattr(client, 'get_payment_url'):
		# For gateways that use redirect-based payment
		payment_details = {
			"amount": flt(details.outstanding_amount),
			"title": _("Payment for {0} course").format(details["outstanding_amount"]),
			"description": _("Payment for {0} course").format(details["outstanding_amount"]),
			"reference_doctype": "Sales Invoice",
			"reference_docname": details["name"],
			"payer_email": frappe.session.user,
			"payer_name": frappe.db.get_value("User", frappe.session.user, "full_name"),
			"currency": details["currency"],
			"payment_gateway": gateway_name,
		}
		
		if phone:
			payment_details["contact"] = phone
			
		options["payment_url"] = client.get_payment_url(**payment_details)
	
	return options


def create_payment_record(args, status, gateway_name=None):
	"""Create payment record for any gateway"""
	if not gateway_name:
		gateway_name = get_payment_gateway()
		
	payment_record = frappe.new_doc("Payment Record")
	payment_record.gateway = gateway_name
	payment_record.status = status
	payment_record.amount = args.get("outstanding_amount", "")
	payment_record.against_invoice = args.get("name", "")
	
	# Set gateway-specific fields
	if gateway_name == "Razorpay":
		payment_record.order_id = args.get("razorpay_order_id", "")
		payment_record.payment_id = args.get("razorpay_payment_id", "")
		payment_record.signature = args.get("razorpay_signature", "")
	elif gateway_name == "PayPal":
		payment_record.order_id = args.get("payment_id", "")
		payment_record.payment_id = args.get("payer_id", "")
	elif gateway_name == "Stripe":
		payment_record.payment_id = args.get("payment_intent", "")
		payment_record.order_id = args.get("session_id", "")
	
	# Common fields for successful payments
	if status == "Completed" or status == "Captured":
		payment_record.student = args.get("student", "")
		payment_record.mobile = args.get("mobile_number", "")
		payment_record.email = args.get("email", "")
		payment_record.address_line_1 = args.get("address_line_1", "")
		payment_record.currency = args.get("currency", "")
		payment_record.address_line_2 = args.get("address_line_2", "")
		payment_record.city = args.get("city", "")
		payment_record.state = args.get("state", "")
		payment_record.country = args.get("country", "")
		payment_record.pincode = args.get("pincode", "")
	elif status == "Failed":
		payment_record.failure_description = args.get("description", "") or args.get("error", {}).get("message", "")
	
	payment_record.save(ignore_permissions=True)
	return payment_record


@frappe.whitelist()
def handle_payment_success(response, against_invoice, billing_details):
	if not response:
		frappe.throw(_("Invalid payment response"))
	
	gateway_name = get_payment_gateway()
	client = get_gateway_client(gateway_name)
	
	# Verify payment signature if the gateway supports it
	if hasattr(client, 'verify_payment_signature'):
		client.utility.verify_payment_signature(response)
	
	payment_details = get_details(against_invoice)
	
	payment_record = create_payment_record(
		{**response, **billing_details, **payment_details}, 
		"Completed",
		gateway_name
	)
	
	try:
		frappe.flags.ignore_account_permission = True
		pe = get_payment_entry("Sales Invoice", against_invoice)
		pe.reference_no = response.get("razorpay_order_id") or response.get("payment_id") or response.get("id")
		pe.reference_date = nowdate()
		pe.posting_date = nowdate()
		pe.save(ignore_permissions=True)
		pe.submit()
	
	except Exception as e:
		frappe.throw(_("Error during payment: {0}").format(e))


@frappe.whitelist()
def handle_payment_failure(response, against_invoice, billing_details):
	if not response:
		frappe.throw(_("Invalid payment response"))
	
	gateway_name = get_payment_gateway()
	payment_details = get_details(against_invoice)
	
	# Extract error details based on gateway
	error_details = {}
	if gateway_name == "Razorpay":
		error_details = response.get("error", {})
		razorpay_data = {
			"description": error_details.get("description"),
			"razorpay_order_id": response.get("metadata", {}).get("order_id"),
			"razorpay_payment_id": response.get("metadata", {}).get("payment_id"),
		}
		payment_record = create_payment_record(
			{**razorpay_data, **billing_details, **payment_details, **error_details}, 
			"Failed",
			gateway_name
		)
	else:
		# Generic handling for other gateways
		payment_record = create_payment_record(
			{**response, **billing_details, **payment_details}, 
			"Failed",
			gateway_name
		)