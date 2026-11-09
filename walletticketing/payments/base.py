from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass

from ..models import Order


class PaymentError(RuntimeError):
    pass


class PaymentState(str, enum.Enum):
    PAID = "paid"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True)
class PaymentResult:
    state: PaymentState
    reference: str = ""


@dataclass(frozen=True)
class CheckoutUrls:
    return_url: str  # Rückkehr nach Zahlung (Bestätigung beim Provider abfragen)
    cancel_url: str  # Abbruch durch Kunden
    order_url: str  # Bestellübersicht


@dataclass(frozen=True)
class WebhookEvent:
    reference: str
    result: PaymentResult


class PaymentProvider(ABC):
    code: str
    label: str
    description: str = ""

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def start(self, order: Order, urls: CheckoutUrls) -> str:
        """Startet die Zahlung. Setzt ggf. `order.payment_reference` und liefert die Redirect-URL."""

    def confirm(self, order: Order, params: Mapping[str, str]) -> PaymentResult:
        """Wird bei Rückkehr des Kunden aufgerufen und prüft den Zahlungsstatus beim Provider."""
        return PaymentResult(PaymentState.PENDING, order.payment_reference)

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookEvent | None:
        return None

    def customer_instructions(self, order: Order) -> dict | None:
        """Optionale Zahlungsanweisungen für die Bestellseite (z.B. Bankdaten)."""
        return None
