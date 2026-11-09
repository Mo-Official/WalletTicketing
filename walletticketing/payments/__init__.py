from __future__ import annotations

import httpx

from ..config import Settings
from ..models import LandingPage
from .base import (
    CheckoutUrls,
    PaymentError,
    PaymentProvider,
    PaymentResult,
    PaymentState,
    WebhookEvent,
)
from .demo import DemoProvider
from .invoice import InvoiceProvider
from .paypal import PayPalProvider
from .stripe import StripeProvider

__all__ = [
    "CheckoutUrls",
    "PaymentError",
    "PaymentProvider",
    "PaymentRegistry",
    "PaymentResult",
    "PaymentState",
    "WebhookEvent",
]


class PaymentRegistry:
    """Hält alle Zahlungsanbieter. Neue Anbieter einfach hier registrieren."""

    def __init__(
        self,
        settings: Settings,
        stripe_client: httpx.Client | None = None,
        paypal_client: httpx.Client | None = None,
    ) -> None:
        self.providers: dict[str, PaymentProvider] = {}
        for provider in (
            StripeProvider(settings, stripe_client),
            PayPalProvider(settings, paypal_client),
            InvoiceProvider(settings),
            DemoProvider(settings),
        ):
            self.register(provider)

    def register(self, provider: PaymentProvider) -> None:
        self.providers[provider.code] = provider

    def get(self, code: str) -> PaymentProvider | None:
        provider = self.providers.get(code)
        return provider if provider and provider.is_available() else None

    def available(self) -> list[PaymentProvider]:
        return [p for p in self.providers.values() if p.is_available()]

    def for_page(self, page: LandingPage) -> list[PaymentProvider]:
        allowed = page.payment_method_list
        return [p for p in self.available() if not allowed or p.code in allowed]
