"""Separate models file — but main.py defines its own models."""

# These models are unused because main.py has its own ORM models
# This demonstrates the god module problem — models.py exists but
# main.py doesn't use it

class UserSchema:
    def __init__(self, name: str, email: str):
        self.name = name
        self.email = email

class ProductSchema:
    def __init__(self, name: str, price: float):
        self.name = name
        self.price = price
