from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Konfiguration über Umgebungsvariablen bzw. `.env`-Datei."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Allgemein
    app_name: str = "WalletTicketing"
    organization_name: str = "Mein Veranstalter"
    base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./walletticketing.db"
    secret_key: str = "change-me-in-production"
    timezone: str = "Europe/Berlin"
    default_currency: str = "EUR"

    # Admin-Login
    admin_username: str = "admin"
    admin_password: str = "admin"

    # API-Key für Einlass-Scanner (Bearer Token)
    scanner_api_key: str = ""

    # Zahlungsarten
    demo_payments: bool = True  # Testzahlung ohne echten Provider (nur Entwicklung!)
    invoice_enabled: bool = True  # Vorkasse / Überweisung
    invoice_iban: str = "DE00 0000 0000 0000 0000 00"
    invoice_bic: str = ""
    invoice_account_holder: str = "Mein Veranstalter GmbH"
    pending_order_minutes: int = 30  # Reservierung für unbezahlte Online-Bestellungen

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_payment_method_types: str = ""  # leer = Stripe entscheidet (Karte, Apple Pay, Google Pay, Klarna, ...)

    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_sandbox: bool = True

    # Apple Wallet
    apple_pass_type_id: str = ""  # z.B. pass.com.example.ticket
    apple_team_id: str = ""
    apple_cert_path: str = ""  # Pass Type ID Zertifikat (PEM)
    apple_key_path: str = ""  # privater Schlüssel (PEM)
    apple_key_password: str = ""
    apple_wwdr_cert_path: str = ""  # Apple WWDR Intermediate (PEM)

    # Google Wallet
    google_issuer_id: str = ""
    google_service_account_file: str = ""  # JSON-Key des Service Accounts

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def apple_wallet_enabled(self) -> bool:
        return all(
            [
                self.apple_pass_type_id,
                self.apple_team_id,
                self.apple_cert_path,
                self.apple_key_path,
                self.apple_wwdr_cert_path,
            ]
        )

    @property
    def google_wallet_enabled(self) -> bool:
        return bool(self.google_issuer_id and self.google_service_account_file)


@lru_cache
def get_settings() -> Settings:
    return Settings()
