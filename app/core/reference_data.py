"""Static reference lists served to the front-end for its dropdowns.

Kept here rather than in a schema module because they are shared data, not
request/response shapes — customers and suppliers can use the same country list.
"""

from zoneinfo import available_timezones

# Company Master dropdowns.
BUSINESS_TYPES = [
    "Private Limited", "Public Limited", "LLP", "Partnership", "Proprietorship", "NGO",
    "One Person Company", "Trust", "Society", "HUF",
]

INDUSTRIES = [
    "Agriculture", "Automotive", "Banking & Finance", "Chemicals", "Construction",
    "Consulting", "Consumer Goods", "E-commerce", "Education", "Electronics",
    "Energy & Utilities", "Engineering", "Entertainment & Media", "Fashion & Apparel",
    "Food & Beverage", "Government", "Healthcare", "Hospitality", "Insurance",
    "Information Technology", "Legal", "Logistics & Transport", "Manufacturing",
    "Mining", "Non-Profit", "Pharmaceuticals", "Real Estate", "Retail", "Telecom",
    "Textiles", "Tourism & Travel", "Wholesale & Distribution", "Other",
]

# ISO 4217 codes for the currencies a firm here is realistically billing in.
CURRENCIES = [
    "INR", "USD", "EUR", "GBP", "AED", "SGD", "AUD", "CAD", "JPY", "CNY", "CHF",
    "HKD", "MYR", "NZD", "SAR", "QAR", "KWD", "OMR", "BHD", "LKR", "NPR", "BDT",
    "THB", "ZAR", "RUB", "BRL",
]

LANGUAGES = [
    "English", "Hindi", "Marathi", "Gujarati", "Bengali", "Tamil", "Telugu",
    "Kannada", "Malayalam", "Punjabi", "Odia", "Assamese", "Urdu",
]

# IANA zone names, straight from the stdlib database rather than hard-coded, so
# the list stays correct as tzdata is updated.
TIME_ZONES = sorted(available_timezones())

BANK_NAMES = [
    "State Bank of India", "HDFC Bank", "ICICI Bank", "Axis Bank", "Kotak Mahindra Bank",
    "Punjab National Bank", "Bank of Baroda", "Canara Bank", "Union Bank of India",
    "Bank of India", "Indian Bank", "Central Bank of India", "Indian Overseas Bank",
    "UCO Bank", "Bank of Maharashtra", "Punjab & Sind Bank", "IDBI Bank", "Yes Bank",
    "IndusInd Bank", "IDFC First Bank", "Federal Bank", "South Indian Bank",
    "Karnataka Bank", "RBL Bank", "Bandhan Bank", "AU Small Finance Bank", "Other",
]

# ISO 3166-1 country names.
COUNTRIES = [
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Antigua and Barbuda", "Argentina",
    "Armenia", "Australia", "Austria", "Azerbaijan", "Bahamas", "Bahrain", "Bangladesh", "Barbados",
    "Belarus", "Belgium", "Belize", "Benin", "Bhutan", "Bolivia", "Bosnia and Herzegovina",
    "Botswana", "Brazil", "Brunei", "Bulgaria", "Burkina Faso", "Burundi", "Cabo Verde", "Cambodia",
    "Cameroon", "Canada", "Central African Republic", "Chad", "Chile", "China", "Colombia",
    "Comoros", "Congo", "Congo (Democratic Republic)", "Costa Rica", "Côte d'Ivoire", "Croatia",
    "Cuba", "Cyprus", "Czechia", "Denmark", "Djibouti", "Dominica", "Dominican Republic", "Ecuador",
    "Egypt", "El Salvador", "Equatorial Guinea", "Eritrea", "Estonia", "Eswatini", "Ethiopia",
    "Fiji", "Finland", "France", "Gabon", "Gambia", "Georgia", "Germany", "Ghana", "Greece",
    "Grenada", "Guatemala", "Guinea", "Guinea-Bissau", "Guyana", "Haiti", "Honduras", "Hungary",
    "Iceland", "India", "Indonesia", "Iran", "Iraq", "Ireland", "Israel", "Italy", "Jamaica",
    "Japan", "Jordan", "Kazakhstan", "Kenya", "Kiribati", "Kuwait", "Kyrgyzstan", "Laos", "Latvia",
    "Lebanon", "Lesotho", "Liberia", "Libya", "Liechtenstein", "Lithuania", "Luxembourg",
    "Madagascar", "Malawi", "Malaysia", "Maldives", "Mali", "Malta", "Marshall Islands",
    "Mauritania", "Mauritius", "Mexico", "Micronesia", "Moldova", "Monaco", "Mongolia",
    "Montenegro", "Morocco", "Mozambique", "Myanmar", "Namibia", "Nauru", "Nepal", "Netherlands",
    "New Zealand", "Nicaragua", "Niger", "Nigeria", "North Korea", "North Macedonia", "Norway",
    "Oman", "Pakistan", "Palau", "Palestine", "Panama", "Papua New Guinea", "Paraguay", "Peru",
    "Philippines", "Poland", "Portugal", "Qatar", "Romania", "Russia", "Rwanda",
    "Saint Kitts and Nevis", "Saint Lucia", "Saint Vincent and the Grenadines", "Samoa",
    "San Marino", "Sao Tome and Principe", "Saudi Arabia", "Senegal", "Serbia", "Seychelles",
    "Sierra Leone", "Singapore", "Slovakia", "Slovenia", "Solomon Islands", "Somalia",
    "South Africa", "South Korea", "South Sudan", "Spain", "Sri Lanka", "Sudan", "Suriname",
    "Sweden", "Switzerland", "Syria", "Taiwan", "Tajikistan", "Tanzania", "Thailand", "Timor-Leste",
    "Togo", "Tonga", "Trinidad and Tobago", "Tunisia", "Türkiye", "Turkmenistan", "Tuvalu",
    "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom", "United States", "Uruguay",
    "Uzbekistan", "Vanuatu", "Vatican City", "Venezuela", "Vietnam", "Yemen", "Zambia", "Zimbabwe",
]

# States / union territories of India — the Address "State" dropdown. Firms
# outside India can still type anything; the field is free text.
INDIAN_STATES = [
    "Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar",
    "Chandigarh", "Chhattisgarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Goa",
    "Gujarat", "Haryana", "Himachal Pradesh", "Jammu and Kashmir", "Jharkhand", "Karnataka",
    "Kerala", "Ladakh", "Lakshadweep", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Puducherry", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
]


# --- Allowed values from the field sheet ------------------------------------
# Closed lists (the sheet names every value) are validated; lists the sheet ends
# with "etc." stay free text and these act as suggestions for the dropdown.

# Lower-case, matching the Customer Profile spec's own example values.
CUSTOMER_STATUSES = ["active", "inactive", "blacklisted", "prospect"]
CUSTOMER_TYPES = ["individual", "business", "government", "dealer", "distributor", "vendor"]

SALES_TYPES = ["Quotation", "Sales Order", "Invoice", "POS Sale", "Return", "Credit Note"]
SALES_STATUSES = ["Draft", "Confirmed", "Completed", "Cancelled", "Returned"]
INVOICE_PAYMENT_STATUSES = ["Unpaid", "Partial", "Paid", "Refunded"]

ORDER_STATUSES_SHEET = ["Draft", "Confirmed", "Processing", "Completed", "Cancelled"]

PURCHASE_TYPES = ["Purchase Order", "Direct Purchase", "Service Purchase", "Asset Purchase"]
PURCHASE_STATUSES = ["Draft", "Ordered", "Received", "Invoiced", "Paid", "Cancelled"]
RECEIVING_STATUSES = ["Pending", "Partial", "Completed"]
PURCHASE_PAYMENT_STATUSES = ["Unpaid", "Partial", "Paid"]

EXPENSE_STATUSES_SHEET = ["Draft", "Submitted", "Approved", "Rejected", "Paid"]
EXPENSE_PAYMENT_STATUSES = ["Pending", "Partially Paid", "Paid"]

APPROVAL_STATUSES = ["Pending", "Approved", "Rejected"]

RETURN_TYPES = ["Refund", "Replacement", "Credit Note"]
RETURN_STATUSES = ["Requested", "Approved", "Refunded", "Closed"]

# Open lists — the sheet ends these with "etc.", so they are suggestions only.
PAYMENT_METHODS = ["Cash", "Card", "UPI", "Bank Transfer", "Cheque", "Wallet", "Credit"]
EXPENSE_TYPES = ["Operational", "Capital", "Reimbursable", "Petty Cash", "Travel", "Utilities"]
INVOICE_STATUSES = ["Draft", "Issued", "Paid", "Cancelled"]
UNITS_OF_MEASURE = ["Piece", "Box", "Kg", "Gram", "Litre", "Millilitre", "Metre", "Dozen", "Pack", "Set"]
