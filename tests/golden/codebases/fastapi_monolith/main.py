"""God module — all routes, models, and business logic in one file."""

from fastapi import FastAPI
from sqlalchemy import create_engine, Column, Integer, String, Float
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# No rate limiting
# No authentication on admin endpoints

DATABASE_URL = "sqlite:///./app.db"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    email = Column(String)
    role = Column(String, default="user")

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    price = Column(Float)

app = FastAPI(title="Monolith API")

# Database queries directly in route handlers — no service layer

@app.get("/users")
def list_users():
    db = SessionLocal()
    users = db.query(User).all()
    db.close()
    return [{"id": u.id, "name": u.name, "email": u.email} for u in users]

@app.post("/users")
def create_user(data: dict):
    # No input validation — raw dict access
    db = SessionLocal()
    user = User(name=data["name"], email=data["email"])
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    return {"id": user.id, "name": user.name}

@app.get("/admin/users")
def admin_list_users():
    # No authentication check!
    db = SessionLocal()
    users = db.query(User).all()
    db.close()
    return [{"id": u.id, "name": u.name, "email": u.email, "role": u.role} for u in users]

@app.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: int):
    # No authentication check!
    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        db.delete(user)
        db.commit()
    db.close()
    return {"deleted": True}

@app.get("/products")
def list_products():
    db = SessionLocal()
    products = db.query(Product).all()
    db.close()
    return [{"id": p.id, "name": p.name, "price": p.price} for p in products]

@app.post("/products")
def create_product(data: dict):
    db = SessionLocal()
    product = Product(name=data["name"], price=data["price"])
    db.add(product)
    db.commit()
    db.refresh(product)
    db.close()
    return {"id": product.id, "name": product.name}

# Circular import reference
from utils import format_price  # noqa: E402
