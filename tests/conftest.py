from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from walletticketing.config import Settings
from walletticketing.db import utcnow
from walletticketing.main import create_app
from walletticketing.models import LandingPage, Product, ValidityType


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        base_url="http://testserver",
        secret_key="test-secret",
        admin_username="admin",
        admin_password="secret",
        scanner_api_key="scan-key",
        demo_payments=True,
        invoice_enabled=True,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(app):
    session = app.state.db.session_factory()
    yield session
    session.close()


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token not found"
    return match.group(1)


@pytest.fixture
def make_product(db):
    def _make(**kwargs) -> Product:
        now = utcnow()
        defaults = dict(
            name="Konzert",
            slug=f"konzert-{len(db.query(Product).all()) + 1}",
            price_cents=2500,
            currency="EUR",
            validity_type=ValidityType.FIXED,
            valid_from=now + timedelta(days=5),
            valid_until=now + timedelta(days=5, hours=4),
            venue="Stadthalle",
        )
        defaults.update(kwargs)
        product = Product(**defaults)
        db.add(product)
        db.commit()
        return product

    return _make


@pytest.fixture
def make_page(db):
    def _make(products, **kwargs) -> LandingPage:
        defaults = dict(slug="sommer", title="Sommerfest", headline="Das Fest des Jahres", published=True)
        defaults.update(kwargs)
        page = LandingPage(**defaults)
        page.products = list(products)
        db.add(page)
        db.commit()
        return page

    return _make


@pytest.fixture
def admin_client(client):
    html = client.get("/admin/login").text
    resp = client.post(
        "/admin/login",
        data={"username": "admin", "password": "secret", "csrf_token": csrf_from(html), "next": "/admin"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return client
