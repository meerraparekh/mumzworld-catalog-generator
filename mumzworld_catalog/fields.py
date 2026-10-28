"""The internal catalog record and how field values carry their provenance.

Every product field is a dict ("field value", FV)::

    {"value": ..., "original": ..., "source": "vendor:Product Name",
     "transform": "trimmed spaces", "reason": "...", "class": "directly_sourced",
     "status": "ok", "locked": False, "history": [...]}

``class`` is the decision classification required for every decided field.
``locked`` marks a value a reviewer set or approved: automatic re-runs never
change a locked value.
"""
from __future__ import annotations

from .sheets import norm

# Decision classifications (section 4E).
CLASSES = {
    "directly_sourced": "Directly sourced",
    "rule_based": "Rule-based",
    "mapped": "Mapped",
    "judgment": "Cataloging judgment",
    "vendor_query": "Vendor query required",
    "manual_review": "Manual review required",
    "manual": "Reviewer set",
}

# Internal vendor-data fields and the header names they are recognised by.
# Recognition is only a proposal: the ticket page shows the column mapping and
# the user can change it before processing.
SOURCE_FIELDS = {
    "sku": ["sku", "vendor sku", "seller sku", "item sku", "supplier sku", "item code", "article number",
            "article no", "product code", "style code", "model sku", "merchant sku", "sku code"],
    "brand": ["brand", "brand name", "manufacturer brand"],
    "product_name": ["product name", "name", "item name", "title", "product title", "description short",
                     "item description", "product"],
    "barcode": ["barcode", "ean", "upc", "gtin", "ean code", "upc code", "ean13", "ean/upc", "barcode number",
                "gtin13", "ean number"],
    "model_number": ["model", "model number", "model no", "mpn", "manufacturer part number"],
    "product_type": ["product type", "type", "item type"],
    "vendor_category": ["category", "vendor category", "product category", "sub category", "subcategory",
                        "department", "class"],
    "colour": ["colour", "color", "colour name", "color name"],
    "size": ["size", "size name"],
    "dimensions": ["dimensions", "product dimensions", "size dimensions", "dimension"],
    "length": ["length", "length cm", "product length"],
    "width": ["width", "width cm", "product width"],
    "height": ["height", "height cm", "product height"],
    "weight": ["weight", "product weight", "net weight", "weight kg", "gross weight", "item weight"],
    "material": ["material", "materials", "fabric", "composition"],
    "age_group": ["age", "age group", "age range", "suitable age", "recommended age", "age suitability"],
    "gender": ["gender", "target gender", "sex"],
    "description": ["description", "long description", "product description", "full description",
                    "marketing description"],
    "features": ["features", "key features", "bullet points", "highlights", "benefits", "selling points"],
    "specifications": ["specifications", "specs", "technical specifications", "product specifications"],
    "safety_info": ["safety", "safety information", "warnings", "safety warnings", "certifications",
                    "certification", "safety standard"],
    "country_of_origin": ["country of origin", "origin", "coo", "made in", "manufactured in"],
    "pack_quantity": ["pack size", "pack quantity", "quantity per pack", "units per pack", "pieces", "count"],
    "contents": ["in the box", "box contents", "whats in the box", "contents", "included accessories"],
    "cost_price": ["cost", "cost price", "unit cost", "supplier price", "buying price", "wholesale price"],
    "retail_price": ["rrp", "retail price", "price", "selling price", "msrp", "recommended retail price"],
    "currency": ["currency"],
    "stock_qty": ["stock", "qty", "quantity", "stock qty", "available quantity", "inventory", "soh"],
    "warranty": ["warranty", "guarantee"],
    "image_refs": ["image", "images", "image url", "image urls", "image link", "image links", "main image",
                   "image file", "image filename", "picture", "photo"],
}

# Fields produced by cataloging (never read from the vendor file directly).
DERIVED_FIELDS = ["category", "category_id", "title", "short_description", "long_description",
                  "bullet_points", "search_terms", "tags"]

LABELS = {
    "sku": "SKU", "brand": "Brand", "product_name": "Product name", "barcode": "Barcode (EAN/UPC/GTIN)",
    "model_number": "Model number", "product_type": "Product type", "vendor_category": "Vendor category",
    "colour": "Colour", "size": "Size", "dimensions": "Dimensions", "length": "Length", "width": "Width",
    "height": "Height", "weight": "Weight", "material": "Material", "age_group": "Age group",
    "gender": "Gender / audience", "description": "Vendor description", "features": "Vendor features",
    "specifications": "Specifications", "safety_info": "Safety / compliance", "country_of_origin":
    "Country of origin", "pack_quantity": "Pack quantity", "contents": "Box contents", "cost_price": "Cost price",
    "retail_price": "Retail price", "currency": "Currency", "stock_qty": "Stock quantity", "warranty": "Warranty",
    "image_refs": "Image references", "category": "Category", "category_id": "Category ID", "title": "Title",
    "short_description": "Short description", "long_description": "Long description",
    "bullet_points": "Key features (bullets)", "search_terms": "Search terms", "tags": "Tags",
}

# Values that must never be reformatted beyond trimming.
CRITICAL_FIELDS = {"sku", "barcode", "model_number", "cost_price", "retail_price", "stock_qty", "pack_quantity",
                   "weight", "length", "width", "height", "dimensions"}


def label(field: str) -> str:
    if field.startswith("attr:"):
        return field[5:]
    return LABELS.get(field, field.replace("_", " ").capitalize())


def resolve_field_name(name: str) -> str | None:
    """Map a free-text field name (e.g. from a mapping sheet or response file) to an
    internal field id.  ``attr:<Attribute>`` names are passed through."""
    if not name:
        return None
    s = str(name).strip()
    if s.lower().startswith("attr:"):
        return "attr:" + s[5:].strip()
    k = norm(s)
    for f in list(SOURCE_FIELDS) + DERIVED_FIELDS:
        if norm(f) == k or norm(LABELS.get(f, "")) == k:
            return f
    for f, syns in SOURCE_FIELDS.items():
        if k in {norm(x) for x in syns}:
            return f
    return None


def propose_column_map(columns: list[str]) -> dict[str, str]:
    """Proposed {source column: internal field}; first exact synonym wins, each
    internal field is used once."""
    out, used = {}, set()
    for col in columns:
        k = norm(col)
        for field, syns in SOURCE_FIELDS.items():
            if field in used:
                continue
            if k == norm(field) or k in {norm(s) for s in syns}:
                out[col] = field
                used.add(field)
                break
    return out


def fv(value, *, source: str, cls: str, original=None, transform: str = "", reason: str = "",
       status: str = "ok", **extra) -> dict:
    d = {"value": value, "original": original if original is not None else value, "source": source,
         "transform": transform, "reason": reason, "class": cls, "status": status, "locked": False,
         "history": []}
    d.update(extra)
    return d


def value(product: dict, field: str, default=""):
    f = product.get("fields", {}).get(field)
    if not f:
        return default
    v = f.get("value")
    return default if v in (None, "") else v


def set_field(product: dict, field: str, new: dict, at: str, force: bool = False) -> bool:
    """Store ``new`` unless the current value is locked.  The replaced value goes to
    history.  Returns True if stored."""
    fields = product.setdefault("fields", {})
    old = fields.get(field)
    if old and old.get("locked") and not force:
        return False
    if old:
        same = (old.get("value") == new.get("value") and old.get("class") == new.get("class")
                and old.get("source") == new.get("source") and old.get("status") == new.get("status"))
        if same:
            return True
        hist = list(old.get("history", []))
        hist.append({"at": at, "value": old.get("value"), "source": old.get("source"), "class": old.get("class"),
                     "reason": old.get("reason", "")})
        new["history"] = hist[-30:]
        if "original" not in new or new.get("original") is None:
            new["original"] = old.get("original")
    fields[field] = new
    return True
