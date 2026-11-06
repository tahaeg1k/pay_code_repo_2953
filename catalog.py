"""Server-side product catalog for the demo shops. Prices here are authoritative: an agent picks product ids
and quantities, and the merchant side fills in the prices, so an agent can never set its own price.
Keep in sync with CATALOG in static/store.html."""
CATALOG = {
    "greenleaf": [
        ("apples-6", "🍎", "Apples (6)", 2.4, "Fruit & veg"), ("avocados-2", "🥑", "Avocados (2)", 2.0, "Fruit & veg"),
        ("broccoli", "🥦", "Broccoli", 0.9, "Fruit & veg"), ("sourdough", "🍞", "Sourdough loaf", 3.2, "Bakery"),
        ("cheddar-400", "🧀", "Cheddar 400g", 4.5, "Dairy"), ("oat-milk", "🥛", "Oat milk 1L", 1.8, "Dairy"),
        ("pasta-500", "🍝", "Pasta 500g", 1.1, "Pantry"), ("coffee-beans", "☕", "Coffee beans", 6.5, "Pantry"),
        ("dark-chocolate", "🍫", "Dark chocolate", 2.2, "Pantry")],
    "northside": [
        ("usb-c-cable", "🔌", "USB-C cable", 8.99, "Cables"), ("power-bank", "🔋", "Power bank", 19.5, "Power"),
        ("earbuds", "🎧", "Earbuds", 24.99, "Audio"), ("wireless-mouse", "🖱️", "Wireless mouse", 14.0, "Desk"),
        ("keyboard", "⌨️", "Keyboard", 34.0, "Desk"), ("smart-bulb", "💡", "Smart bulb", 9.5, "Home")],
}


def products(merchant_id):
    return [{"product_id": pid, "emoji": e, "name": n, "price": p, "category": cat}
            for pid, e, n, p, cat in CATALOG.get(merchant_id, [])]


def build_basket(merchant_id, items):
    """[{product_id, qty}] -> priced basket lines. Raises ValueError for unknown products or bad quantities."""
    known = {p["product_id"]: p for p in products(merchant_id)}
    if not known:
        raise ValueError(f"Unknown shop {merchant_id!r}")
    basket = []
    for it in items:
        p, qty = known.get(it.get("product_id")), int(it.get("qty", 1))
        if not p:
            raise ValueError(f"{it.get('product_id')!r} is not sold at this shop")
        if not 1 <= qty <= 50:
            raise ValueError("Quantity must be between 1 and 50")
        basket.append({"name": p["name"], "qty": qty, "price": p["price"]})
    return basket
