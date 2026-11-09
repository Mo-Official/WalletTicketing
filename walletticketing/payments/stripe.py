"""Stripe Checkout – Kreditkarte, Apple Pay, Google Pay, SEPA, Klarna, ... (je nach Stripe-Konto)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping

import httpx

from ..config import Settings
from ..models import Order
from .base import CheckoutUrls, PaymentError, PaymentProvider, PaymentResult, PaymentState, WebhookEvent

STRIPE_API = "https://api.stripe.com"
WEBHOOK_TOLERANCE_SECONDS = 300


class StripeProvider(PaymentProvider):
    code = "stripe"
    label = "Kreditkarte, Apple Pay, Google Pay & mehr"
    description = "Sichere Zahlung über Stripe"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=STRIPE_API, timeout=20)
        return self._client

    def is_available(self) -> bool:
        return bool(self.settings.stripe_secret_key)

    def _auth(self) -> tuple[str, str]:
        return (self.settings.stripe_secret_key, "")

    def start(self, order: Order, urls: CheckoutUrls) -> str:
        data: dict[str, str] = {
            "mode": "payment",
            "success_url": f"{urls.return_url}?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": urls.cancel_url,
            "customer_email": order.customer_email,
            "client_reference_id": order.public_token,
            "metadata[order_number]": order.number,
        }
        for i, item in enumerate(order.items):
            prefix = f"line_items[{i}]"
            data[f"{prefix}[quantity]"] = str(item.quantity)
            data[f"{prefix}[price_data][currency]"] = order.currency.lower()
            data[f"{prefix}[price_data][unit_amount]"] = str(item.unit_price_cents)
            name = item.product.name
            if item.visit_date:
                name += f" ({item.visit_date:%d.%m.%Y})"
            data[f"{prefix}[price_data][product_data][name]"] = name
        methods = [m.strip() for m in self.settings.stripe_payment_method_types.split(",") if m.strip()]
        for i, method in enumerate(methods):
            data[f"payment_method_types[{i}]"] = method

        resp = self.client.post("/v1/checkout/sessions", data=data, auth=self._auth())
        if resp.status_code >= 400:
            raise PaymentError(f"Stripe-Fehler: {resp.text[:300]}")
        session = resp.json()
        order.payment_reference = session["id"]
        return session["url"]

    def confirm(self, order: Order, params: Mapping[str, str]) -> PaymentResult:
        session_id = params.get("session_id") or order.payment_reference
        if session_id != order.payment_reference:
            return PaymentResult(PaymentState.FAILED, session_id)
        resp = self.client.get(f"/v1/checkout/sessions/{session_id}", auth=self._auth())
        if resp.status_code >= 400:
            raise PaymentError(f"Stripe-Fehler: {resp.text[:300]}")
        return self._result_from_session(resp.json())

    @staticmethod
    def _result_from_session(session: dict) -> PaymentResult:
        payment_status = session.get("payment_status")
        if payment_status in ("paid", "no_payment_required"):
            state = PaymentState.PAID
        elif session.get("status") == "expired":
            state = PaymentState.FAILED
        else:
            state = PaymentState.PENDING  # z.B. SEPA-Lastschrift in Bearbeitung
        return PaymentResult(state, session["id"])

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent | None:
        secret = self.settings.stripe_webhook_secret
        if not secret:
            raise PaymentError("Stripe Webhook-Secret ist nicht konfiguriert")
        verify_stripe_signature(body, headers.get("stripe-signature", ""), secret)
        event = json.loads(body)
        if event.get("type") not in (
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed",
            "checkout.session.expired",
        ):
            return None
        session = event["data"]["object"]
        result = self._result_from_session(session)
        if event["type"] == "checkout.session.async_payment_failed":
            result = PaymentResult(PaymentState.FAILED, session["id"])
        return WebhookEvent(reference=session["id"], result=result)


def verify_stripe_signature(body: bytes, header: str, secret: str, now: float | None = None) -> None:
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        key, _, value = item.partition("=")
        parts.setdefault(key.strip(), []).append(value.strip())
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError) as exc:
        raise PaymentError("Ungültige Stripe-Signatur") from exc
    if abs((now or time.time()) - timestamp) > WEBHOOK_TOLERANCE_SECONDS:
        raise PaymentError("Stripe-Signatur ist abgelaufen")
    signed = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise PaymentError("Ungültige Stripe-Signatur")
