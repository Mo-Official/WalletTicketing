import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from walletticketing.main import create_app
from walletticketing.models import Order, OrderStatus
from walletticketing.payments import PaymentError, PaymentRegistry
from walletticketing.payments.stripe import verify_stripe_signature

from .conftest import csrf_from


class FakeStripe:
    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.last_form: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Basic ")
        if request.method == "POST" and request.url.path == "/v1/checkout/sessions":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.last_form = form
            sid = f"cs_test_{len(self.sessions) + 1}"
            self.sessions[sid] = {"id": sid, "url": f"https://checkout.stripe.com/c/{sid}", "payment_status": "unpaid", "status": "open"}
            return httpx.Response(200, json=self.sessions[sid])
        if request.method == "GET" and request.url.path.startswith("/v1/checkout/sessions/"):
            sid = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=self.sessions[sid])
        return httpx.Response(404)


class FakePayPal:
    def __init__(self):
        self.status = "APPROVED"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "A21"})
        assert request.headers["authorization"] == "Bearer A21"
        if path == "/v2/checkout/orders" and request.method == "POST":
            body = json.loads(request.content)
            assert body["purchase_units"][0]["amount"] == {"currency_code": "EUR", "value": "50.00"}
            return httpx.Response(201, json={"id": "PP-1", "links": [{"rel": "payer-action", "href": "https://paypal.test/approve/PP-1"}]})
        if path == "/v2/checkout/orders/PP-1/capture":
            self.status = "COMPLETED"
            return httpx.Response(201, json={"id": "PP-1", "status": "COMPLETED"})
        return httpx.Response(404)


@pytest.fixture
def provider_env(settings):
    s = settings.model_copy(update=dict(
        stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_test",
        paypal_client_id="id", paypal_client_secret="secret",
    ))
    fake_stripe, fake_paypal = FakeStripe(), FakePayPal()
    registry = PaymentRegistry(
        s,
        stripe_client=httpx.Client(base_url="https://api.stripe.com", transport=httpx.MockTransport(fake_stripe.handler)),
        paypal_client=httpx.Client(base_url="https://api-m.sandbox.paypal.com", transport=httpx.MockTransport(fake_paypal.handler)),
    )
    app = create_app(s, registry)
    with TestClient(app) as client:
        yield app, client, fake_stripe, fake_paypal


def _setup_page(app):
    from datetime import timedelta

    from walletticketing.db import utcnow
    from walletticketing.models import LandingPage, Product, ValidityType

    db = app.state.db.session_factory()
    now = utcnow()
    p = Product(name="Show", slug="show", price_cents=2500, validity_type=ValidityType.FIXED,
                valid_from=now + timedelta(days=1), valid_until=now + timedelta(days=1, hours=2))
    page = LandingPage(slug="show", title="Show", published=True, products=[p])
    db.add(page)
    db.commit()
    return db, p


def _checkout(client, product, method):
    html = client.get("/l/show").text
    return client.post("/l/show/checkout", data={
        "csrf_token": csrf_from(html), f"qty_{product.id}": "2", "name": "Max", "email": "max@example.com",
        "payment_method": method,
    }, follow_redirects=False)


def test_all_methods_offered(provider_env):
    app, client, *_ = provider_env
    _setup_page(app)
    html = client.get("/l/show").text
    for code in ("stripe", "paypal", "invoice", "demo"):
        assert f'value="{code}"' in html


def test_stripe_checkout_and_return(provider_env):
    app, client, fake, _ = provider_env
    db, p = _setup_page(app)
    resp = _checkout(client, p, "stripe")
    assert resp.headers["location"] == "https://checkout.stripe.com/c/cs_test_1"
    assert fake.last_form["line_items[0][price_data][unit_amount]"] == "2500"
    assert fake.last_form["line_items[0][quantity]"] == "2"
    assert "{CHECKOUT_SESSION_ID}" in fake.last_form["success_url"]

    order = db.scalar(select(Order))
    assert order.payment_reference == "cs_test_1"

    # Rückkehr, Zahlung noch offen
    client.get(f"/checkout/{order.public_token}/return?session_id=cs_test_1")
    db.refresh(order)
    assert order.status == OrderStatus.PENDING

    fake.sessions["cs_test_1"]["payment_status"] = "paid"
    # Manipulierte Session-ID wird abgelehnt
    client.get(f"/checkout/{order.public_token}/return?session_id=cs_test_999")
    db.refresh(order)
    assert order.status == OrderStatus.CANCELLED


def test_stripe_return_paid(provider_env):
    app, client, fake, _ = provider_env
    db, p = _setup_page(app)
    _checkout(client, p, "stripe")
    order = db.scalar(select(Order))
    fake.sessions["cs_test_1"]["payment_status"] = "paid"
    page = client.get(f"/checkout/{order.public_token}/return?session_id=cs_test_1").text
    assert "Zahlung ist eingegangen" in page
    db.refresh(order)
    assert order.status == OrderStatus.PAID and len(order.tickets) == 2


def _signed(body: bytes, secret="whsec_test", ts=None):
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_stripe_webhook(provider_env):
    app, client, fake, _ = provider_env
    db, p = _setup_page(app)
    _checkout(client, p, "stripe")
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_test_1", "payment_status": "paid"}}}).encode()

    assert client.post("/webhooks/stripe", content=body, headers={"stripe-signature": "t=1,v1=bad"}).status_code == 400
    resp = client.post("/webhooks/stripe", content=body, headers={"stripe-signature": _signed(body)})
    assert resp.status_code == 204
    order = db.scalar(select(Order))
    db.refresh(order)
    assert order.status == OrderStatus.PAID and len(order.tickets) == 2
    # Idempotent
    assert client.post("/webhooks/stripe", content=body, headers={"stripe-signature": _signed(body)}).status_code == 204
    db.refresh(order)
    assert len(order.tickets) == 2


def test_stripe_signature_expired():
    body = b"{}"
    with pytest.raises(PaymentError):
        verify_stripe_signature(body, _signed(body, ts=1000), "whsec_test", now=5000)


def test_paypal_flow(provider_env):
    app, client, _, fake = provider_env
    db, p = _setup_page(app)
    resp = _checkout(client, p, "paypal")
    assert resp.headers["location"] == "https://paypal.test/approve/PP-1"
    order = db.scalar(select(Order))
    assert order.payment_reference == "PP-1"
    client.get(f"/checkout/{order.public_token}/return?token=PP-1&PayerID=X")
    db.refresh(order)
    assert order.status == OrderStatus.PAID and len(order.tickets) == 2


def test_cancel_url_releases_order(provider_env):
    app, client, *_ = provider_env
    db, p = _setup_page(app)
    _checkout(client, p, "paypal")
    order = db.scalar(select(Order))
    resp = client.get(f"/checkout/{order.public_token}/cancel", follow_redirects=False)
    assert resp.headers["location"] == "/l/show"
    db.refresh(order)
    assert order.status == OrderStatus.CANCELLED
