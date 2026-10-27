from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="rewritten pricing")


class Cart(BaseModel):
    prices: list[int]
    coupon: str = ""


@app.post("/total")
def total(cart: Cart):
    subtotal = sum(cart.prices)
    # "Simplified" during the rewrite: the threshold became strict.
    if cart.coupon == "HALF" and subtotal > 100:
        subtotal = subtotal // 2
    return {"total": subtotal, "items": len(cart.prices)}
