import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from walletticketing.main import create_app
from walletticketing.models import ValidityType
from walletticketing.services import LineRequest, create_order, mark_paid


def _cert(subject: str, key, issuer_key=None, issuer_name=None, ca=False):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer_name or name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    return builder.sign(issuer_key or key, hashes.SHA256())


@pytest.fixture
def wallet_settings(settings, tmp_path):
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = _cert("Test WWDR", ca_key, ca=True)
    pass_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pass_cert = _cert("Pass Type ID: pass.test.ticket", pass_key, ca_key, ca.subject)
    (tmp_path / "wwdr.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "pass.pem").write_bytes(pass_cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "pass.key").write_bytes(
        pass_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )

    google_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sa = {
        "type": "service_account",
        "client_email": "wallet@test.iam.gserviceaccount.com",
        "private_key_id": "kid123",
        "private_key": google_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
    }
    (tmp_path / "sa.json").write_text(json.dumps(sa))

    s = settings.model_copy(
        update=dict(
            apple_pass_type_id="pass.test.ticket",
            apple_team_id="TEAM123456",
            apple_cert_path=str(tmp_path / "pass.pem"),
            apple_key_path=str(tmp_path / "pass.key"),
            apple_wwdr_cert_path=str(tmp_path / "wwdr.pem"),
            google_issuer_id="3388000000012345678",
            google_service_account_file=str(tmp_path / "sa.json"),
        )
    )
    return s


@pytest.fixture
def wallet_env(wallet_settings):
    app = create_app(wallet_settings)
    with TestClient(app) as client:
        yield app, client, wallet_settings


def _ticket(app, settings, **product_kwargs):
    from walletticketing.models import Product

    db = app.state.db.session_factory()
    now = datetime.now(timezone.utc)
    defaults = dict(
        name="Jazz Night", slug="jazz", price_cents=1500, validity_type=ValidityType.FIXED,
        valid_from=now + timedelta(days=2), valid_until=now + timedelta(days=2, hours=3), venue="Club", color="#222222",
    )
    defaults.update(product_kwargs)
    product = Product(**defaults)
    db.add(product)
    db.commit()
    order = create_order(
        db, settings, page=None, customer_name="Max Muster", customer_email="m@example.com",
        lines=[LineRequest(product, 1)], payment_method="demo",
    )
    db.commit()
    mark_paid(db, order, settings)
    return order.tickets[0], db


def _read_pkpass(content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def test_apple_event_ticket(wallet_env):
    app, client, settings = wallet_env
    ticket, _ = _ticket(app, settings)
    resp = client.get(f"/t/{ticket.access_token}/apple.pkpass")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.apple.pkpass"

    files = _read_pkpass(resp.content)
    assert {"pass.json", "manifest.json", "signature", "icon.png", "logo.png"} <= files.keys()
    manifest = json.loads(files["manifest.json"])
    for name, digest in manifest.items():
        assert hashlib.sha1(files[name]).hexdigest() == digest

    certs = pkcs7.load_der_pkcs7_certificates(files["signature"])
    subjects = {c.subject.rfc4514_string() for c in certs}
    assert "CN=Test WWDR" in subjects

    p = json.loads(files["pass.json"])
    assert p["passTypeIdentifier"] == "pass.test.ticket" and p["teamIdentifier"] == "TEAM123456"
    assert "eventTicket" in p and "generic" not in p
    assert p["barcodes"][0]["message"] == ticket.code
    assert p["expirationDate"].endswith("Z") and p["voided"] is False
    assert p["relevantDates"][0]["startDate"] == p["relevantDate"]


def test_apple_season_pass_uses_generic_style(wallet_env):
    app, client, settings = wallet_env
    ticket, _ = _ticket(app, settings, slug="monat", validity_type=ValidityType.DURATION, validity_days=30,
                        valid_from=None, valid_until=None, multi_entry=True)
    files = _read_pkpass(client.get(f"/t/{ticket.access_token}/apple.pkpass").content)
    p = json.loads(files["pass.json"])
    assert "generic" in p and "eventTicket" not in p
    assert p["generic"]["headerFields"][0]["key"] == "until"


def test_apple_voided_after_cancel(wallet_env):
    from walletticketing.services import cancel_order

    app, client, settings = wallet_env
    ticket, db = _ticket(app, settings)
    cancel_order(db, ticket.order)
    p = json.loads(_read_pkpass(client.get(f"/t/{ticket.access_token}/apple.pkpass").content)["pass.json"])
    assert p["voided"] is True


def _google_claims(client, ticket, settings):
    resp = client.get(f"/t/{ticket.access_token}/google", follow_redirects=False)
    assert resp.status_code == 303
    url = resp.headers["location"]
    assert url.startswith("https://pay.google.com/gp/v/save/")
    token = urlparse(url).path.rsplit("/", 1)[-1]
    assert jwt.get_unverified_header(token)["kid"] == "kid123"
    sa = json.loads(open(settings.google_service_account_file).read())
    public_key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None).public_key()
    return jwt.decode(token, public_key, algorithms=["RS256"], audience="google")


def test_google_event_ticket(wallet_env):
    app, client, settings = wallet_env
    ticket, _ = _ticket(app, settings)
    claims = _google_claims(client, ticket, settings)
    assert claims["typ"] == "savetowallet" and claims["origins"] == ["http://testserver"]
    obj = claims["payload"]["eventTicketObjects"][0]
    cls = claims["payload"]["eventTicketClasses"][0]
    assert obj["classId"] == cls["id"] and obj["id"].startswith("3388000000012345678.")
    assert obj["barcode"]["value"] == ticket.code
    assert obj["state"] == "ACTIVE"
    assert "start" in obj["validTimeInterval"] and "end" in obj["validTimeInterval"]


def test_google_season_pass_is_generic(wallet_env):
    app, client, settings = wallet_env
    ticket, _ = _ticket(app, settings, slug="monat", validity_type=ValidityType.DURATION, validity_days=30,
                        valid_from=None, valid_until=None)
    payload = _google_claims(client, ticket, settings)["payload"]
    assert "genericObjects" in payload and "eventTicketObjects" not in payload
    assert payload["genericObjects"][0]["header"]["defaultValue"]["value"] == "Jazz Night"


def test_wallet_buttons_shown_when_configured(wallet_env):
    app, client, settings = wallet_env
    ticket, _ = _ticket(app, settings)
    html = client.get(f"/t/{ticket.access_token}", headers={"user-agent": "Mozilla/5.0 (Linux; Android 14)"}).text
    assert "Zu Google Wallet hinzufügen" in html and "Zu Apple Wallet hinzufügen" in html
    assert html.index("Google Wallet") < html.index("Apple Wallet")  # Android: Google zuerst


def test_wallet_not_configured_returns_503(client, db, settings, make_product):
    p = make_product()
    order = create_order(db, settings, page=None, customer_name="A", customer_email="a@example.com",
                         lines=[LineRequest(p, 1)], payment_method="demo")
    db.commit()
    mark_paid(db, order, settings)
    token = order.tickets[0].access_token
    assert client.get(f"/t/{token}/apple.pkpass").status_code == 503
    assert client.get(f"/t/{token}/google", follow_redirects=False).status_code == 503
    assert "nicht konfiguriert" in client.get(f"/t/{token}").text
