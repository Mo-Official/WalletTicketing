"""PayPal Checkout über die Orders v2 API."""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from ..config import Settings
from ..models import Order
from .base import CheckoutUrls, PaymentError, PaymentProvider, PaymentResult, PaymentState


class PayPalProvider(PaymentProvider):
    code = "paypal"
    label = "PayPal"
    description = "Bezahlen mit PayPal"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def api_base(self) -> str:
        return "https://api-m.sandbox.paypal.com" if self.settings.paypal_sandbox else "https://api-m.paypal.com"

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.api_base, timeout=20)
        return self._client

    def is_available(self) -> bool:
        return bool(self.settings.paypal_client_id and self.settings.paypal_client_secret)

    def _access_token(self) -> str:
        resp = self.client.post(
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(self.settings.paypal_client_id, self.settings.paypal_client_secret),
        )
        if resp.status_code >= 400:
            raise PaymentError(f"PayPal-Authentifizierung fehlgeschlagen: {resp.text[:300]}")
        return resp.json()["access_token"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}

    def start(self, order: Order, urls: CheckoutUrls) -> str:
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": order.public_token[:256],
                    "custom_id": order.number,
                    "description": f"Bestellung {order.number}",
                    "amount": {"currency_code": order.currency, "value": f"{order.total_cents / 100:.2f}"},
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": self.settings.organization_name[:127],
                        "user_action": "PAY_NOW",
                        "shipping_preference": "NO_SHIPPING",
                        "return_url": urls.return_url,
                        "cancel_url": urls.cancel_url,
                    }
                }
            },
        }
        resp = self.client.post(
            "/v2/checkout/orders",
            json=payload,
            headers={**self._headers(), "PayPal-Request-Id": f"create-{order.public_token}"},
        )
        if resp.status_code >= 400:
            raise PaymentError(f"PayPal-Fehler: {resp.text[:300]}")
        data = resp.json()
        order.payment_reference = data["id"]
        for link in data.get("links", []):
            if link.get("rel") in ("payer-action", "approve"):
                return link["href"]
        raise PaymentError("PayPal lieferte keinen Freigabe-Link")

    def confirm(self, order: Order, params: Mapping[str, str]) -> PaymentResult:
        paypal_order_id = params.get("token") or order.payment_reference
        if paypal_order_id != order.payment_reference:
            return PaymentResult(PaymentState.FAILED, paypal_order_id)
        resp = self.client.post(
            f"/v2/checkout/orders/{paypal_order_id}/capture",
            headers={**self._headers(), "PayPal-Request-Id": f"capture-{order.public_token}"},
        )
        if resp.status_code == 422:
            # Bereits erfasst? Status direkt abfragen.
            resp = self.client.get(f"/v2/checkout/orders/{paypal_order_id}", headers=self._headers())
        if resp.status_code >= 400:
            raise PaymentError(f"PayPal-Fehler: {resp.text[:300]}")
        status = resp.json().get("status")
        if status == "COMPLETED":
            return PaymentResult(PaymentState.PAID, paypal_order_id)
        if status in ("VOIDED",):
            return PaymentResult(PaymentState.FAILED, paypal_order_id)
        return PaymentResult(PaymentState.PENDING, paypal_order_id)
