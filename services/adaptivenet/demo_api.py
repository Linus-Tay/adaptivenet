"""Identical stateless demo workload deployed in each region."""
import os
from fastapi import FastAPI

app = FastAPI(title="AdaptiveNet demo catalogue")

@app.get("/healthz")
def health():
    return {"status": "ok"}

@app.get("/api/catalog")
def catalog():
    return {"region": os.getenv("REGION", "local"), "items": [
        {"id": "coffee", "name": "Coffee", "price": 4.5},
        {"id": "tea", "name": "Tea", "price": 3.5},
    ]}
