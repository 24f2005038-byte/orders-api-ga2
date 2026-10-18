import base64
import json
import time
import uuid
from collections import defaultdict, deque
from threading import Lock

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

TOTAL_ORDERS = 52
RATE_LIMIT = 16
WINDOW_SECONDS = 10

ALLOWED_ORIGINS = [
    "https://exam.sanand.workers.dev",
    "https://app-bcx7se.example.com",
]

app = FastAPI()

# In-memory stores: suitable for this single-instance exercise.
rate_buckets = defaultdict(deque)
rate_lock = Lock()

idempotency_store = {}
idempotency_lock = Lock()


class OrderRequest(BaseModel):
    # Optional order details supplied by the client.
    product: str = "default"
    quantity: int = 1


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Let CORS preflight pass without consuming rate-limit tokens.
        if request.method == "OPTIONS":
            return await call_next(request)

        client_id = request.headers.get("X-Client-Id", "anonymous")
        now = time.monotonic()

        with rate_lock:
            bucket = rate_buckets[client_id]

            while bucket and now - bucket[0] >= WINDOW_SECONDS:
                bucket.popleft()

            if len(bucket) >= RATE_LIMIT:
                retry_after = max(
                    1,
                    int(WINDOW_SECONDS - (now - bucket[0]) + 0.999),
                )
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded"},
                    headers={"Retry-After": str(retry_after)},
                )

            bucket.append(now)

        return await call_next(request)


# Add rate limiting first; CORS is added last and wraps it.
app.add_middleware(RateLimitMiddleware)


@app.post("/orders")
async def create_order(
    body: OrderRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    if not idempotency_key.strip():
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key cannot be empty",
        )

    with idempotency_lock:
        if idempotency_key in idempotency_store:
            saved_order = idempotency_store[idempotency_key]
        else:
            saved_order = {
                "id": str(uuid.uuid4()),
                "product": body.product,
                "quantity": body.quantity,
            }
            idempotency_store[idempotency_key] = saved_order

    return JSONResponse(status_code=201, content=saved_order)


def encode_cursor(offset: int) -> str:
    raw = json.dumps({"offset": offset}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded)
        offset = json.loads(raw)["offset"]
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValueError
        if offset < 0 or offset > TOTAL_ORDERS:
            raise ValueError
        return offset
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cursor")


@app.get("/orders")
async def list_orders(
    limit: int = Query(default=10, ge=1, le=52),
    cursor: str | None = None,
):
    offset = decode_cursor(cursor) if cursor else 0
    end = min(offset + limit, TOTAL_ORDERS)

    items = [
        {"id": order_id, "description": f"Order {order_id}"}
        for order_id in range(offset + 1, end + 1)
    ]

    next_cursor = encode_cursor(end) if end < TOTAL_ORDERS else None

    return {
        "items": items,
        "next_cursor": next_cursor,
    }


# CORS wraps the rate limiter, allowing preflight to succeed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Idempotency-Key",
        "X-Client-Id",
    ],
    expose_headers=["Retry-After"],
    max_age=600,
)
