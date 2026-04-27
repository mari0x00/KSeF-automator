#!/usr/bin/env python3
"""
Wysyłka faktury XML do KSeF.

Domyślnie korzysta ze środowiska testowego (TEST) z auto-generowanym certyfikatem.
Flaga --prod przełącza na środowisko produkcyjne (wymaga certyfikatu).

Użycie:
    python send_invoice.py output/A3_3_2026.xml                                          # TEST (auto-cert)
    python send_invoice.py output/A3_3_2026.xml --cert config/cert.crt --key config/cert.key --prod  # PROD
"""

import argparse
import sys
from pathlib import Path

from lxml import etree
from ksef2 import Client, Environment
from ksef2.core.xades import load_certificate_from_pem, load_private_key_from_pem
from ksef2.domain.models import FormSchema
from ksef2.core import exceptions

import yaml


BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
XSD_DIR = BASE_DIR / "xsd"


def load_seller():
    with open(CONFIG_DIR / "seller.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_xsd(xml_path):
    """Walidacja XML przeciwko schematowi FA(3)."""
    if not (XSD_DIR / "schemat.xsd").exists():
        print("UWAGA: Brak plików XSD w katalogu xsd/ — pomijam walidację lokalną.")
        return True

    class LocalResolver(etree.Resolver):
        def resolve(self, url, public_id, context):
            for f in XSD_DIR.glob("*.xsd"):
                if url.endswith(f.name):
                    return self.resolve_filename(str(f), context)
            return None

    parser = etree.XMLParser()
    parser.resolvers.add(LocalResolver())
    xsd_doc = etree.parse(str(XSD_DIR / "schemat.xsd"), parser)
    schema = etree.XMLSchema(xsd_doc)
    xml_doc = etree.parse(str(xml_path))

    if schema.validate(xml_doc):
        print("XSD: VALID")
        return True

    print("XSD: INVALID — faktura nie przeszła walidacji schematu FA(3):")
    for error in schema.error_log:
        print(f"  Linia {error.line}: {error.message}")
    return False


def send_invoice(xml_path, cert_path, key_path, key_password, environment):
    """Wysyłka faktury do KSeF."""
    seller = load_seller()
    nip = seller["nip"]

    is_prod = environment == Environment.PRODUCTION
    env_name = "PRODUCTION" if is_prod else "TEST"
    print(f"Środowisko: {env_name}")
    print(f"NIP: {nip}")
    print(f"Faktura: {xml_path}")
    print()

    client = Client(environment=environment)

    try:
        if is_prod:
            # Produkcja — wymagany certyfikat
            cert = load_certificate_from_pem(str(cert_path))
            password = key_password.encode() if key_password else None
            private_key = load_private_key_from_pem(str(key_path), password=password)
            print("Certyfikat załadowany.")
            print("Autentykacja XAdES...")
            auth = client.authentication.with_xades(
                nip=nip,
                cert=cert,
                private_key=private_key,
                verify_chain=True,
            )
        else:
            # Test — auto-generowany certyfikat
            print("Autentykacja (certyfikat testowy)...")
            auth = client.authentication.with_test_certificate(nip=nip)

        print("Autentykacja OK.")

        # Otwórz sesję i wyślij fakturę
        invoice_xml = Path(xml_path).read_bytes()

        with auth.online_session(form_code=FormSchema.FA3) as session:
            print("Sesja otwarta. Wysyłanie faktury...")

            status = session.send_invoice_and_wait(
                invoice_xml=invoice_xml,
                timeout=120.0,
                poll_interval=3.0,
            )

            if status.ksef_number:
                print(f"\nSUKCES!")
                print(f"  Numer KSeF: {status.ksef_number}")
                print(f"  Reference:  {status.reference_number}")

                # Pobierz UPO
                try:
                    upo = session.get_invoice_upo_by_ksef_number(
                        ksef_number=status.ksef_number
                    )
                    upo_path = xml_path.parent / f"{xml_path.stem}_UPO.xml"
                    upo_path.write_bytes(upo)
                    print(f"  UPO:        {upo_path}")
                except Exception as e:
                    print(f"  UPO: nie udało się pobrać ({e})")
            else:
                print(f"\nBŁĄD: Faktura nie została przetworzona.")
                print(f"  Kod:  {status.status.code}")
                print(f"  Opis: {status.status.description}")
                if status.status.details:
                    print(f"  Szczegóły: {status.status.details}")
                sys.exit(1)

    except exceptions.KSeFAuthError as e:
        print(f"\nBŁĄD autentykacji: {e}")
        sys.exit(1)
    except exceptions.KSeFInvoiceProcessingTimeoutError:
        print("\nBŁĄD: Timeout — faktura nie została przetworzona w wymaganym czasie.")
        print("Sprawdź status ręcznie w portalu KSeF.")
        sys.exit(1)
    except exceptions.KSeFApiError as e:
        print(f"\nBŁĄD API KSeF: {e}")
        sys.exit(1)
    except exceptions.KSeFException as e:
        print(f"\nBŁĄD KSeF: {e}")
        sys.exit(1)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Wysyłka faktury XML do KSeF"
    )
    parser.add_argument("xml", type=Path, help="Ścieżka do pliku XML faktury")
    parser.add_argument("--cert", type=Path, default=None, help="Ścieżka do certyfikatu (.crt) — wymagane dla --prod")
    parser.add_argument("--key", type=Path, default=None, help="Ścieżka do klucza prywatnego (.key) — wymagane dla --prod")
    parser.add_argument("--key-password", default=None, help="Hasło do klucza (jeśli zaszyfrowany)")
    parser.add_argument("--prod", action="store_true", help="Wyślij na środowisko PRODUKCYJNE (domyślnie: TEST)")
    parser.add_argument("--skip-validation", action="store_true", help="Pomiń walidację XSD przed wysyłką")
    args = parser.parse_args()

    # Sprawdź pliki
    if not args.xml.exists():
        print(f"Błąd: plik {args.xml} nie istnieje.")
        sys.exit(1)

    if args.prod:
        if not args.cert or not args.key:
            print("Błąd: --prod wymaga --cert i --key.")
            sys.exit(1)
        if not args.cert.exists():
            print(f"Błąd: certyfikat {args.cert} nie istnieje.")
            sys.exit(1)
        if not args.key.exists():
            print(f"Błąd: klucz {args.key} nie istnieje.")
            sys.exit(1)

    # Walidacja XSD
    if not args.skip_validation:
        if not validate_xsd(args.xml):
            print("\nFaktura nie przeszła walidacji. Użyj --skip-validation aby wymusić wysyłkę.")
            sys.exit(1)

    # Potwierdzenie dla PROD
    environment = Environment.PRODUCTION if args.prod else Environment.TEST
    if args.prod:
        print("=" * 50)
        print("  UWAGA: ŚRODOWISKO PRODUKCYJNE!")
        print("  Faktura zostanie wysłana do prawdziwego KSeF.")
        print("=" * 50)
        answer = input("Kontynuować? [t/N]: ").strip().lower()
        if answer != "t":
            print("Anulowano.")
            sys.exit(0)

    send_invoice(args.xml, args.cert, args.key, args.key_password, environment)


if __name__ == "__main__":
    main()
