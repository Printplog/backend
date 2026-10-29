"""
Default names for purchased documents.

When a template carries a person's name (Passenger_Name, Full_Name, Surname +
Given_Name, ...) the document is named after that person, e.g.
"John Doe - Boarding Pass", instead of the generic "My Boarding Pass".
"""
import re
from typing import Iterable, Optional

# Roles whose "<role>_name" field holds the document owner's name
PERSON_ROLES = {
    "passenger", "passanger", "holder", "cardholder", "card", "account", "customer",
    "client", "recipient", "receiver", "consignee", "applicant", "student", "employee",
    "staff", "patient", "owner", "beneficiary", "traveller", "traveler", "guest", "buyer",
    "member", "candidate", "payee", "insured", "policyholder", "driver", "licensee",
    "bearer", "tenant", "person", "full", "your", "my", "legal", "display", "of", "fan",
}

# Name fields that describe something other than the document owner
EXCLUDED_WORDS = {
    "company", "bank", "business", "file", "product", "item", "hotel", "airline",
    "airport", "school", "university", "college", "merchant", "store", "shop", "brand",
    "street", "city", "state", "country", "user", "username", "father", "mother",
    "spouse", "kin", "nok", "emergency", "doctor", "agent", "officer", "issuer",
    "sender", "shipper", "vessel", "ship", "flight", "event", "venue", "org",
    "organization", "organisation", "institution", "employer", "hospital", "desc",
    "description", "label", "title", "project", "team", "app", "site", "domain",
    "carrier", "courier", "station", "terminal", "gate", "seat", "plan", "package",
    "service", "manager", "signatory", "witness", "guardian", "parent",
    "reference", "ref", "email", "phone", "address", "maiden", "nick", "nickname",
}

SKIPPED_TYPES = {
    "select", "checkbox", "hide", "hide_checked", "hide_unchecked", "status", "upload",
    "file", "sign", "qrcode", "barcode", "date", "email", "tel", "number", "range",
    "color", "password", "fixed",
}

GIVEN_WORDS = {"first", "given", "givenname", "givennames", "forename", "forenames", "firstname"}
MIDDLE_WORDS = {"middle", "middlename", "other"}
SURNAME_WORDS = {"last", "surname", "family", "lastname", "familyname", "surnames"}
NAME_WORDS = {"name", "names", "fullname", "fullnames"}


def _words(field_id: str) -> list[str]:
    # "Passenger_Name1" -> ["passenger", "name"]; "GivenNames" -> ["given", "names"]
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", field_id or "")
    return [w for w in re.split(r"[^a-z]+", spaced.lower()) if w]


def _suffix_number(field_id: str) -> int:
    match = re.search(r"(\d+)\D*$", field_id or "")
    return int(match.group(1)) if match else 0


def _clean_value(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip(" ,;:-")
    if not (2 <= len(text) <= 80):
        return None
    if "@" in text or "http" in text.lower():
        return None
    letters = sum(ch.isalpha() for ch in text)
    digits = sum(ch.isdigit() for ch in text)
    if letters == 0 or digits > 0:
        return None
    return text


def _classify(field: dict) -> Optional[str]:
    if field.get("type") in SKIPPED_TYPES or field.get("options"):
        return None
    if field.get("dependsOn"):
        return None
    words = _words(str(field.get("id", "")))
    if not words or EXCLUDED_WORDS.intersection(words):
        return None
    word_set = set(words)
    if word_set & SURNAME_WORDS:
        return "surname"
    has_name = bool(word_set & NAME_WORDS) or "firstname" in word_set
    if word_set & GIVEN_WORDS and (has_name or word_set & {"forename", "forenames"}):
        return "given"
    if word_set & MIDDLE_WORDS and has_name:
        return "middle"
    if has_name:
        other = word_set - NAME_WORDS
        # "Name", "Full_Name", "Passenger_Name" - but not "Product_Name"
        if not other or other <= PERSON_ROLES:
            return "full"
    return None


def person_name_from_fields(form_fields: Iterable[dict]) -> Optional[str]:
    """Find the document owner's name among filled-in fields, if any."""
    found: dict[str, list[tuple[int, int, str]]] = {}
    for index, field in enumerate(form_fields or []):
        if not isinstance(field, dict):
            continue
        kind = _classify(field)
        if not kind:
            continue
        text = _clean_value(field.get("currentValue"))
        if text:
            # Prefer "Name"/"Name1" over "Name2", then template order
            found.setdefault(kind, []).append((_suffix_number(str(field.get("id"))), index, text))

    def first(kind: str) -> Optional[str]:
        return min(found[kind])[2] if found.get(kind) else None

    if first("full"):
        return first("full")
    parts = [first("given"), first("middle"), first("surname")]
    if parts[0] or parts[2]:
        return " ".join(p for p in parts if p)
    return None


def default_document_name(template, form_fields: Iterable[dict] = ()) -> str:
    """'<Person> - <Template>' when a name is found, else 'My <Template>'."""
    if template is None:
        return "Untitled Document"
    person = person_name_from_fields(form_fields)
    if person:
        return f"{person} - {template.name}"[:255]
    return f"My {template.name}"[:255]
