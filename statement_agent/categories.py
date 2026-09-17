"""Spending categories: the built-in list, how a file's own labels map onto it, and whole-word keywords.

Categories are open-ended. A label a file uses ("Fitness", "Home Maintenance") is kept as a category of its
own when it isn't simply another name for a built-in one ("Dining Out" -> Dining, "Grocery Shopping" ->
Groceries). Nothing here calls a model; groq_categorize.py handles merchants none of this recognises.
"""

from __future__ import annotations

import re

from .corrections import merchant_words

BUILT_IN = [
    "Dining", "Groceries", "Transport", "Travel", "Entertainment", "Subscriptions", "Utilities", "Shopping",
    "Healthcare", "Personal Care", "Fitness", "Education", "Housing", "Insurance", "Gifts & Donations",
    "Home Maintenance", "Other",
]

# a file's label (normalized: lowercase, "&" -> "and", punctuation removed) -> built-in category
_ALIASES = {
    "dining": "Dining", "dining out": "Dining", "eating out": "Dining", "restaurants": "Dining",
    "restaurant": "Dining", "food delivery": "Dining", "takeaway": "Dining", "food and dining": "Dining",
    "groceries": "Groceries", "grocery": "Groceries", "grocery shopping": "Groceries", "supermarket": "Groceries",
    "transport": "Transport", "transportation": "Transport", "commute": "Transport", "fuel": "Transport",
    "gas": "Transport", "gasoline": "Transport", "auto and transport": "Transport", "taxi": "Transport",
    "travel": "Travel", "vacation": "Travel", "flights": "Travel", "hotels": "Travel",
    "entertainment": "Entertainment", "recreation": "Entertainment", "movies": "Entertainment",
    "leisure": "Entertainment", "fun": "Entertainment",
    "subscription": "Subscriptions", "subscriptions": "Subscriptions", "streaming": "Subscriptions",
    "software": "Subscriptions", "memberships": "Subscriptions",
    "utilities": "Utilities", "bills": "Utilities", "bills and utilities": "Utilities", "communication": "Utilities",
    "phone": "Utilities", "internet": "Utilities", "electricity": "Utilities", "mobile": "Utilities",
    "shopping": "Shopping", "clothing": "Shopping", "apparel": "Shopping", "electronics": "Shopping",
    "health": "Healthcare", "healthcare": "Healthcare", "medical": "Healthcare", "pharmacy": "Healthcare",
    "doctor": "Healthcare", "health and wellness": "Healthcare",
    "personal care": "Personal Care", "beauty": "Personal Care", "grooming": "Personal Care",
    "fitness": "Fitness", "gym": "Fitness", "sports": "Fitness", "health and fitness": "Fitness",
    "education": "Education", "tuition": "Education", "courses": "Education", "books": "Education",
    "rent": "Housing", "housing": "Housing", "mortgage": "Housing", "home": "Housing",
    "insurance": "Insurance",
    "gifts": "Gifts & Donations", "gift": "Gifts & Donations", "donations": "Gifts & Donations",
    "charity": "Gifts & Donations", "gifts and donations": "Gifts & Donations",
    "home maintenance": "Home Maintenance", "repairs": "Home Maintenance", "home improvement": "Home Maintenance",
    "household": "Home Maintenance",
    "miscellaneous": "Other", "misc": "Other", "other": "Other", "others": "Other", "general": "Other",
    "uncategorized": None, "uncategorised": None, "unknown": None, "": None,
}

# whole words (or word pairs) in a description -> category; checked after the older substring list
WORD_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("Dining", ("lunch", "dinner", "breakfast", "brunch", "meal", "food court", "dominos", "mcdonalds", "kfc",
                "starbucks", "subway", "burger", "biryani", "canteen", "eatery")),
    ("Groceries", ("vegetables", "fruits", "grocery", "groceries", "blinkit", "zepto", "instamart", "milk",
                   "dairy", "supermarket")),
    ("Transport", ("uber", "ola", "lyft", "taxi", "cab", "metro", "bus", "train", "parking", "toll", "fastag",
                   "petrol", "diesel", "fuel")),
    ("Travel", ("flight", "airline", "airways", "hotel", "irctc", "booking", "expedia", "airbnb", "visa fee")),
    ("Entertainment", ("movie", "cinema", "bowling", "concert", "theatre", "theater", "game", "gaming", "steam",
                       "playstation", "xbox", "museum", "bookmyshow")),
    ("Subscriptions", ("subscription", "netflix", "spotify", "anthropic", "claude", "openai", "chatgpt", "github",
                       "notion", "figma", "canva", "adobe", "microsoft", "icloud", "dropbox", "youtube premium",
                       "prime video", "hotstar", "jiocinema", "zoom", "slack", "monthly plan", "google one",
                       "apple com bill", "patreon", "substack", "linkedin premium", "perplexity")),
    ("Utilities", ("electricity", "electric", "water bill", "gas bill", "phone bill", "mobile bill", "recharge",
                   "broadband", "internet", "wifi", "postpaid", "prepaid", "dth", "tata play", "bescom")),
    ("Shopping", ("amazon", "amzn", "ebay", "flipkart", "myntra", "ajio", "meesho", "nykaa", "walmart",
                  "ikea", "clothing", "apparel", "shoes", "electronics", "stationery", "aliexpress", "etsy",
                  "decathlon", "purchase")),
    ("Healthcare", ("doctor", "consultation", "hospital", "clinic", "pharmacy", "medicine", "medicines",
                    "dental", "dentist", "lab test", "diagnostic", "practo", "1mg", "netmeds")),
    ("Personal Care", ("haircut", "salon", "barber", "spa", "cosmetics", "skincare")),
    ("Fitness", ("gym", "fitness", "cult fit", "cultfit", "yoga", "gym membership")),
    ("Education", ("course", "tuition", "school", "college", "university", "udemy", "coursera", "exam",
                   "books", "book", "kindle", "byjus", "unacademy")),
    ("Housing", ("rent", "mortgage", "maintenance charges", "society", "property tax", "hoa")),
    ("Insurance", ("insurance", "premium", "policy", "lic")),
    ("Gifts & Donations", ("gift", "present", "donation", "charity", "birthday")),
    ("Home Maintenance", ("plumbing", "plumber", "electrician", "carpenter", "repair", "urban company",
                          "pest control", "cleaning")),
]

# a debit that moves money into a wallet you own: a transfer, not spending
WALLET_TOPUP_RE = re.compile(
    r"\b(?:amazon\s*pay|paytm|phonepe|mobikwik|google\s*pay|apple\s*cash|paypal|venmo|revolut)\b.*"
    r"\b(?:balance|wallet|top[\s-]?up|add\s*money|load)\b|\bwallet\s*(?:top[\s-]?up|load|recharge)\b|\badd\s*money\s*to\s*wallet\b",
    re.IGNORECASE,
)


def _norm_label(label: str) -> str:
    text = label.lower().replace("&", " and ")
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def canonical(label: str | None) -> str | None:
    """A file's label as a category: its built-in equivalent if it has one, otherwise the label itself
    (tidied). None for labels that mean "no category"."""
    if label is None:
        return None
    norm = _norm_label(label)
    if norm in _ALIASES:
        return _ALIASES[norm]
    tidy = " ".join(label.split())
    return tidy[:40] if tidy else None


def by_words(text: str | None) -> str | None:
    words = merchant_words(text)
    joined = f" {' '.join(words).lower()} "
    for category, keys in WORD_KEYWORDS:
        if any(f" {k} " in joined for k in keys):
            return category
    return None


def merchant_key(text: str | None) -> str:
    """The merchant as remembered: the description's meaningful words, without numbers or payment codes."""
    return " ".join(merchant_words(text)[:5])
