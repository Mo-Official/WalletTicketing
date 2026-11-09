from __future__ import annotations

from ..config import Settings
from ..models import Order
from .base import CheckoutUrls, PaymentProvider


class DemoProvider(PaymentProvider):
    """Simulierte Zahlung für Entwicklung und Präsentationen. In Produktion deaktivieren!"""

    code = "demo"
    label = "Testzahlung"
    description = "Simulierte Zahlung – nur für Entwicklung"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_available(self) -> bool:
        return self.settings.demo_payments

    def start(self, order: Order, urls: CheckoutUrls) -> str:
        order.payment_reference = f"demo-{order.public_token[:12]}"
        return f"{self.settings.base_url}/checkout/{order.public_token}/demo"
