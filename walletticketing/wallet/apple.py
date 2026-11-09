"""Apple Wallet (.pkpass) Erzeugung inkl. PKCS#7-Signatur."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7
from PIL import Image, ImageDraw

from ..config import Settings
from ..models import WalletStyle
from .common import PassData, WalletNotConfigured, foreground_for, hex_to_rgb

PKPASS_MIME = "application/vnd.apple.pkpass"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rgb(color: tuple[int, int, int]) -> str:
    return "rgb({}, {}, {})".format(*color)


def build_pass_json(data: PassData, settings: Settings) -> dict:
    tz = settings.tz
    start_local = data.valid_from.astimezone(tz)
    end_local = data.valid_until.astimezone(tz)
    # Endet die Gültigkeit um Mitternacht, ist der Vortag der letzte gültige Tag
    ends_at_midnight = end_local.time() == time.min
    last_moment = data.valid_until - timedelta(seconds=1) if ends_at_midnight else data.valid_until
    fg = foreground_for(data.color)

    fields: dict[str, list[dict]] = {
        "headerFields": [],
        "primaryFields": [{"key": "event", "label": "TICKET", "value": data.title}],
        "secondaryFields": [],
        "auxiliaryFields": [],
        "backFields": [],
    }

    if data.style == WalletStyle.EVENT:
        # Event-/Tagesticket: Datum oben, Uhrzeit & Ort darunter
        fields["headerFields"].append(
            {
                "key": "date",
                "label": "DATUM",
                "value": _iso(data.valid_from),
                "dateStyle": "PKDateStyleMedium",
                "timeStyle": "PKDateStyleNone",
                "ignoresTimeZone": False,
            }
        )
        fields["secondaryFields"].append({"key": "validity", "label": "GÜLTIG", "value": data.validity_text})
        if data.venue:
            fields["auxiliaryFields"].append({"key": "venue", "label": "ORT", "value": data.venue})
    else:
        # Zeitkarte: Gültigkeitszeitraum prominent
        fields["headerFields"].append(
            {
                "key": "until",
                "label": "GÜLTIG BIS",
                "value": _iso(last_moment),
                "dateStyle": "PKDateStyleShort",
                "timeStyle": "PKDateStyleNone" if ends_at_midnight else "PKDateStyleShort",
            }
        )
        fields["secondaryFields"].append(
            {"key": "from", "label": "AB", "value": f"{start_local:%d.%m.%Y %H:%M}"}
        )
        fields["secondaryFields"].append(
            {
                "key": "to",
                "label": "BIS",
                "value": f"{last_moment.astimezone(tz):%d.%m.%Y}" if ends_at_midnight else f"{end_local:%d.%m.%Y %H:%M}",
            }
        )
        if data.venue:
            fields["auxiliaryFields"].append({"key": "venue", "label": "ORT", "value": data.venue})

    fields["auxiliaryFields"].append({"key": "holder", "label": "NAME", "value": data.holder})
    fields["backFields"].extend(
        [
            {"key": "code", "label": "Ticketcode", "value": data.barcode},
            {"key": "order", "label": "Bestellung", "value": data.order_number},
            {"key": "validity_full", "label": "Gültigkeit", "value": data.validity_text},
            {
                "key": "entry",
                "label": "Einlass",
                "value": "Mehrfacher Einlass im Gültigkeitszeitraum" if data.multi_entry else "Einmaliger Einlass",
            },
            {"key": "link", "label": "Ticket online", "value": data.ticket_url},
        ]
    )
    if data.description:
        fields["backFields"].append({"key": "description", "label": "Info", "value": data.description})

    style_key = "eventTicket" if data.style == WalletStyle.EVENT else "generic"
    pass_json: dict = {
        "formatVersion": 1,
        "passTypeIdentifier": settings.apple_pass_type_id,
        "teamIdentifier": settings.apple_team_id,
        "serialNumber": data.serial,
        "organizationName": data.organization,
        "description": f"Ticket: {data.title}",
        "logoText": data.organization,
        "backgroundColor": _rgb(hex_to_rgb(data.color)),
        "foregroundColor": _rgb(fg),
        "labelColor": _rgb(fg),
        "barcodes": [
            {
                "format": "PKBarcodeFormatQR",
                "message": data.barcode,
                "messageEncoding": "iso-8859-1",
                "altText": data.barcode,
            }
        ],
        # Wallet zeigt das Ticket zum richtigen Zeitpunkt auf dem Sperrbildschirm
        # und markiert es nach Ablauf automatisch als abgelaufen.
        "relevantDate": _iso(data.valid_from),
        "relevantDates": [{"startDate": _iso(data.valid_from), "endDate": _iso(data.valid_until)}],
        "expirationDate": _iso(data.valid_until),
        "voided": data.voided,
        style_key: fields,
    }
    return pass_json


def _png(size: tuple[int, int], color: str, text: str = "") -> bytes:
    img = Image.new("RGBA", size, (*hex_to_rgb(color), 255))
    if text:
        draw = ImageDraw.Draw(img)
        fg = foreground_for(color)
        draw.text((size[0] // 2, size[1] // 2), text, fill=(*fg, 255), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def default_images(color: str, initial: str) -> dict[str, bytes]:
    return {
        "icon.png": _png((29, 29), color, initial),
        "icon@2x.png": _png((58, 58), color, initial),
        "icon@3x.png": _png((87, 87), color, initial),
        "logo.png": _png((160, 50), color),
        "logo@2x.png": _png((320, 100), color),
    }


class ApplePassSigner:
    def __init__(self, cert_pem: bytes, key_pem: bytes, wwdr_pem: bytes, key_password: bytes | None = None):
        self.cert = x509.load_pem_x509_certificate(cert_pem)
        self.key = serialization.load_pem_private_key(key_pem, password=key_password)
        self.wwdr = x509.load_pem_x509_certificate(wwdr_pem)

    @classmethod
    def from_settings(cls, settings: Settings) -> ApplePassSigner:
        if not settings.apple_wallet_enabled:
            raise WalletNotConfigured("Apple Wallet ist nicht konfiguriert")
        return cls(
            Path(settings.apple_cert_path).read_bytes(),
            Path(settings.apple_key_path).read_bytes(),
            Path(settings.apple_wwdr_cert_path).read_bytes(),
            settings.apple_key_password.encode() or None,
        )

    def sign(self, manifest: bytes) -> bytes:
        return (
            pkcs7.PKCS7SignatureBuilder()
            .set_data(manifest)
            .add_signer(self.cert, self.key, hashes.SHA256())
            .add_certificate(self.wwdr)
            .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary])
        )


def build_pkpass(data: PassData, settings: Settings, signer: ApplePassSigner) -> bytes:
    files: dict[str, bytes] = {
        "pass.json": json.dumps(build_pass_json(data, settings), ensure_ascii=False, indent=2).encode("utf-8"),
        **default_images(data.color, (data.organization[:1] or "T").upper()),
    }
    manifest = json.dumps({name: hashlib.sha1(content).hexdigest() for name, content in files.items()}).encode()
    files["manifest.json"] = manifest
    files["signature"] = signer.sign(manifest)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()
