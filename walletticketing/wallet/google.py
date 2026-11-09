"""Google Wallet "Add to Google Wallet"-Links (signiertes JWT)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import jwt

from ..config import Settings
from ..models import WalletStyle
from ..validity import Phase
from .common import PassData, WalletNotConfigured

SAVE_URL = "https://pay.google.com/gp/v/save/"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def _text(value: str, lang: str = "de") -> dict:
    return {"defaultValue": {"language": lang, "value": value}}


def _state(phase: Phase) -> str:
    return {
        Phase.VOID: "INACTIVE",
        Phase.EXPIRED: "EXPIRED",
    }.get(phase, "ACTIVE")


def build_objects(data: PassData, settings: Settings, product_slug: str) -> dict:
    """Erzeugt Klasse + Objekt für das JWT-Payload abhängig von der Darstellung."""
    issuer = settings.google_issuer_id
    class_id = f"{issuer}.{_safe_id(product_slug)}-{data.style.value}"
    object_id = f"{issuer}.{_safe_id(data.serial)}"

    barcode = {"type": "QR_CODE", "value": data.barcode, "alternateText": data.barcode}
    valid_interval = {"start": {"date": _iso(data.valid_from)}, "end": {"date": _iso(data.valid_until)}}
    text_modules = [
        {"id": "validity", "header": "Gültigkeit", "body": data.validity_text},
        {"id": "order", "header": "Bestellung", "body": data.order_number},
        {
            "id": "entry",
            "header": "Einlass",
            "body": "Mehrfacher Einlass im Gültigkeitszeitraum" if data.multi_entry else "Einmaliger Einlass",
        },
    ]
    if data.description:
        text_modules.append({"id": "info", "header": "Info", "body": data.description})
    links = {"uris": [{"uri": data.ticket_url, "description": "Ticket online anzeigen", "id": "ticket"}]}

    if data.style == WalletStyle.EVENT:
        event_class: dict = {
            "id": class_id,
            "issuerName": data.organization,
            "eventName": _text(data.title),
            "reviewStatus": "UNDER_REVIEW",
            "hexBackgroundColor": data.color,
            "dateTime": {"start": _iso(data.valid_from), "end": _iso(data.valid_until)},
        }
        if data.venue:
            event_class["venue"] = {"name": _text(data.venue), "address": _text(data.venue)}
        event_object = {
            "id": object_id,
            "classId": class_id,
            "state": _state(data.phase),
            "ticketHolderName": data.holder,
            "ticketNumber": data.barcode,
            "barcode": barcode,
            "validTimeInterval": valid_interval,
            "hexBackgroundColor": data.color,
            "textModulesData": text_modules,
            "linksModuleData": links,
        }
        return {"eventTicketClasses": [event_class], "eventTicketObjects": [event_object]}

    generic_class = {"id": class_id}
    generic_object = {
        "id": object_id,
        "classId": class_id,
        "state": _state(data.phase),
        "cardTitle": _text(data.organization),
        "header": _text(data.title),
        "subheader": _text(f"Gültig {data.validity_text}"),
        "barcode": barcode,
        "validTimeInterval": valid_interval,
        "hexBackgroundColor": data.color,
        "textModulesData": [{"id": "holder", "header": "Name", "body": data.holder}, *text_modules],
        "linksModuleData": links,
    }
    return {"genericClasses": [generic_class], "genericObjects": [generic_object]}


class GoogleWalletSigner:
    def __init__(self, service_account: dict) -> None:
        self.email = service_account["client_email"]
        self.private_key = service_account["private_key"]
        self.key_id = service_account.get("private_key_id")

    @classmethod
    def from_settings(cls, settings: Settings) -> GoogleWalletSigner:
        if not settings.google_wallet_enabled:
            raise WalletNotConfigured("Google Wallet ist nicht konfiguriert")
        return cls(json.loads(Path(settings.google_service_account_file).read_text()))

    def save_url(self, payload: dict, origins: list[str]) -> str:
        claims = {
            "iss": self.email,
            "aud": "google",
            "typ": "savetowallet",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "origins": origins,
            "payload": payload,
        }
        headers = {"kid": self.key_id} if self.key_id else None
        token = jwt.encode(claims, self.private_key, algorithm="RS256", headers=headers)
        return SAVE_URL + token
