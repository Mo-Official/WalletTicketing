from __future__ import annotations

from ..config import Settings
from ..models import Order
from .base import CheckoutUrls, PaymentProvider


class InvoiceProvider(PaymentProvider):
    """Vorkasse per Banküberweisung. Der Zahlungseingang wird im Backend bestätigt."""

    code = "invoice"
    label = "Vorkasse / Überweisung"
    description = "Tickets werden nach Zahlungseingang freigeschaltet"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_available(self) -> bool:
        return self.settings.invoice_enabled

    def start(self, order: Order, urls: CheckoutUrls) -> str:
        order.payment_reference = order.number
        return urls.order_url

    def customer_instructions(self, order: Order) -> dict | None:
        return {
            "Kontoinhaber": self.settings.invoice_account_holder,
            "IBAN": self.settings.invoice_iban,
            "BIC": self.settings.invoice_bic,
            "Betrag": f"{order.total:.2f} {order.currency}".replace(".", ","),
            "Verwendungszweck": order.number,
        }
