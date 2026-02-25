"""Utility functions — circular import with main.py."""

from main import Product  # Circular import!

def format_price(price: float) -> str:
    return f"${price:.2f}"

def get_product_summary(product: Product) -> str:
    return f"{product.name}: {format_price(product.price)}"
