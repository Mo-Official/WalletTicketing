# WalletTicketing

Ticketverkauf in Python mit

* **Backend** zur Verwaltung von **Produkten** (Tickets) und **Landing Pages**, Bestellungen und Einlasskontrolle
* **Frontend** mit Landing Pages, Warenkorb und **mehreren Zahlungsarten** (Stripe, PayPal, Vorkasse, Testzahlung)
* **Wallet-Tickets** für **Apple Wallet** (`.pkpass`) und **Google Wallet** – Darstellung abhängig vom **Gültigkeitsraum**

Technik: FastAPI · SQLAlchemy 2 · Jinja2 · SQLite/PostgreSQL · `cryptography` (PKCS#7-Signatur) · PyJWT (Google-Wallet-JWT)

## Schnellstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # Werte anpassen
python -m walletticketing.seed  # optionale Beispieldaten
uvicorn walletticketing.main:create_app --factory --reload
```

* Shop: <http://localhost:8000/>
* Backend: <http://localhost:8000/admin> (Login aus `ADMIN_USERNAME` / `ADMIN_PASSWORD`)
* API-Doku: <http://localhost:8000/api/docs>

Tests: `pytest`

## Funktionsüberblick

### Backend (`/admin`)

| Bereich | Funktionen |
|---|---|
| Dashboard | Umsatz (30 Tage), Bestellungen, Status der Integrationen, Sicherheitswarnungen |
| Produkte | Name, Preis, Währung, Ort, Beschreibung, Farbe (für Wallet), Kontingent, Max. pro Bestellung, Mehrfacheinlass, **Gültigkeitsraum**, Wallet-Darstellung |
| Landing Pages | Titel, Slug, Untertitel, Text, Hero-Bild, Akzentfarbe, zugeordnete Produkte, **erlaubte Zahlungsarten je Seite**, Veröffentlichen |
| Bestellungen | Suche/Filter, Details, Zahlungseingang bestätigen (Vorkasse), Stornieren/Erstatten (Tickets werden ungültig) |
| Einlass | Ticketcode scannen/eingeben → prüft Status + Gültigkeitszeitraum, entwertet (Einfach-/Mehrfacheinlass) |

### Frontend

1. `/l/<slug>` – Landing Page mit Produkten, Anzahl, ggf. Besuchstag, Kundendaten, Zahlungsart
2. Weiterleitung zum Zahlungsanbieter → Rückkehr `/checkout/<token>/return` → Status wird **beim Anbieter verifiziert**
3. `/order/<token>` – Bestellübersicht mit Tickets bzw. Überweisungsdaten
4. `/t/<token>` – Ticket mit QR-Code und Buttons „Zu Apple Wallet / Google Wallet hinzufügen“
   (auf Android wird Google Wallet zuerst angeboten, auf iOS Apple Wallet)

### Zahlungsarten

| Code | Anbieter | Ablauf |
|---|---|---|
| `stripe` | Stripe Checkout – Kreditkarte, Apple Pay, Google Pay, SEPA, Klarna … | Redirect, Prüfung der Session bei Rückkehr + Webhook `/webhooks/stripe` (Signaturprüfung) |
| `paypal` | PayPal Orders API v2 | Redirect, Capture bei Rückkehr |
| `invoice` | Vorkasse / Überweisung | Bankdaten auf Bestellseite, Freigabe im Backend |
| `demo` | Testzahlung | nur für Entwicklung (`DEMO_PAYMENTS=false` in Produktion) |

Eine Zahlungsart ist automatisch verfügbar, sobald ihre Zugangsdaten konfiguriert sind. Neue Anbieter:
Klasse von `payments.base.PaymentProvider` ableiten und in `PaymentRegistry` registrieren.

Unbezahlte Online-Bestellungen reservieren das Kontingent `PENDING_ORDER_MINUTES` lang.

## Gültigkeitsräume & Wallet-Darstellung

| Gültigkeitsraum | Beispiel | Ticket gültig | Wallet-Darstellung (Auto) |
|---|---|---|---|
| **Fester Zeitraum** | Konzert, Festival, Saison | `valid_from` – `valid_until` des Produkts | Event-Ticket |
| **Tag nach Wahl** | Museum, Zoo | gewählter Tag 00:00 – 24:00 (Zeitzone `TIMEZONE`), optional nur im buchbaren Zeitraum | Event-Ticket |
| **Zeitkarte** | Monatskarte, 7-Tage-Pass | ab Kauf bis Ende des N-ten Kalendertags | 1 Tag: Event-Ticket, mehrere Tage: Zeitkarte |

Die Darstellung kann pro Produkt fest auf „Event-Ticket“ oder „Zeit-/Saisonkarte“ gesetzt werden.

| | Event-Ticket | Zeit-/Saisonkarte |
|---|---|---|
| Apple Wallet | Pass-Stil `eventTicket`, Datum im Header | Pass-Stil `generic`, „Gültig bis“ im Header, Ab/Bis-Felder |
| Google Wallet | `EventTicketClass/Object` mit `dateTime` & Veranstaltungsort | `GenericClass/Object` mit Zeitraum im Untertitel |

Beide Wallets erhalten außerdem:

* **Apple:** `relevantDate`/`relevantDates` (Ticket erscheint zur richtigen Zeit auf dem Sperrbildschirm), `expirationDate` (wird danach automatisch als abgelaufen markiert), `voided` bei Storno
* **Google:** `validTimeInterval`, `state` = `ACTIVE` / `EXPIRED` / `INACTIVE`
* QR-Code mit dem Ticketcode, Inhabername, Bestellnummer, Einlassregel, Link zum Online-Ticket

## Wallet einrichten

### Apple Wallet

1. Im Apple Developer Account einen **Pass Type ID** anlegen (z.B. `pass.com.example.ticket`) und Zertifikat erzeugen.
2. Zertifikat + Schlüssel als PEM exportieren:
   ```bash
   openssl pkcs12 -in Certificates.p12 -clcerts -nokeys -out certs/pass.pem -legacy
   openssl pkcs12 -in Certificates.p12 -nocerts -nodes -out certs/pass.key -legacy
   ```
3. Apple **WWDR G4** Zwischenzertifikat laden und als PEM speichern (`certs/wwdr.pem`).
4. `APPLE_PASS_TYPE_ID`, `APPLE_TEAM_ID` und die Pfade in `.env` setzen.

### Google Wallet

1. In der [Google Pay & Wallet Console](https://pay.google.com/business/console) Issuer-Konto anlegen → **Issuer ID**.
2. In Google Cloud einen Service Account mit Zugriff auf die Wallet API anlegen, JSON-Key herunterladen.
3. Service Account in der Wallet Console als Nutzer hinzufügen.
4. `GOOGLE_ISSUER_ID` und `GOOGLE_SERVICE_ACCOUNT_FILE` setzen.

Die „Zu Google Wallet hinzufügen“-Links enthalten Klasse und Objekt direkt im signierten JWT – es sind keine
weiteren API-Aufrufe nötig.

## Einlass-API

```bash
curl -X POST http://localhost:8000/api/check-in \
  -H "Authorization: Bearer $SCANNER_API_KEY" -H "Content-Type: application/json" \
  -d '{"code": "K7QF-3MZX-PA9R-W2HD"}'
```

Antwort: `{"ok": true, "message": "Einlass OK – Open-Air Konzert", "holder": "...", "check_in_count": 1}`

## Projektstruktur

```
walletticketing/
  config.py        Einstellungen (.env)
  models.py        Produkt, LandingPage, Bestellung, Ticket
  validity.py      Gültigkeitsraum, Phase, Wallet-Stil
  services.py      Bestellung anlegen, bezahlen, stornieren, Einlass
  payments/        Stripe, PayPal, Vorkasse, Testzahlung
  wallet/          Apple .pkpass (signiert), Google Wallet JWT
  web/             Routen: Shop, Tickets, Admin, API
  templates/       Jinja2-Templates (Shop + Admin)
  seed.py          Beispieldaten
tests/             pytest-Suite (Zahlungen mit Mock-Transport, Wallet-Signaturen mit Testzertifikaten)
```

## Hinweise für den Produktivbetrieb

* `SECRET_KEY`, `ADMIN_PASSWORD` setzen, `DEMO_PAYMENTS=false`, `BASE_URL` mit `https://`
* PostgreSQL statt SQLite verwenden (`DATABASE_URL`), z.B. `pip install psycopg[binary]`
* Stripe-Webhook auf `{BASE_URL}/webhooks/stripe` einrichten (Events `checkout.session.*`)
* Nicht enthalten / nächste Ausbaustufen: E-Mail-Versand der Bestellbestätigung, Apple-Wallet-Web-Service für
  Push-Updates bereits installierter Pässe, Rechnungs-PDF, Rollen im Backend, Datenbankmigrationen (Alembic)
