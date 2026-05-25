#!/usr/bin/env python3
"""
Generowanie faktury VAT jako PDF + XML kompatybilny z KSeF (schemat FA(3)).

Użycie:
    python generate_invoice.py ALIAS                          # wszystkie wartości z YAML + auto-numer
    python generate_invoice.py ALIAS -a 5000                  # nadpisanie kwoty
    python generate_invoice.py ALIAS --date 2026-05-01 -n A3  # nadpisanie daty i numeru
"""

import argparse
import calendar
import json
import sys
from datetime import datetime, timedelta, date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import yaml
from lxml import etree
from fpdf import FPDF
from num2words import num2words


BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
OUTPUT_DIR = BASE_DIR / "output"
XSD_DIR = BASE_DIR / "xsd"
INVOICE_DB = BASE_DIR / "invoices.yaml"

VAT_RATE = Decimal("23")
VAT_MULTIPLIER = Decimal("0.23")

KSEF_NS = "http://crd.gov.pl/wzor/2025/06/25/13775/"
NSMAP = {
    None: KSEF_NS,
    "etd": "http://crd.gov.pl/xml/schematy/dziedzinowe/mf/2022/01/05/eD/DefinicjeTypy/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

ENGLISH_MONTHS = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

FONT_PATHS = [
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf",
     "/Library/Fonts/Arial Unicode.ttf"),
    (str(BASE_DIR / "fonts" / "DejaVuSans.ttf"),
     str(BASE_DIR / "fonts" / "DejaVuSans-Bold.ttf")),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seller():
    return load_yaml(CONFIG_DIR / "seller.yaml")


def load_contractor(alias):
    data = load_yaml(CONFIG_DIR / "contractors.yaml")
    if alias not in data:
        print(f"Błąd: kontrahent '{alias}' nie znaleziony w contractors.yaml")
        print(f"Dostępne aliasy: {', '.join(data.keys())}")
        sys.exit(1)
    return data[alias]


def calculate(net_amount):
    net = Decimal(str(net_amount)).quantize(Decimal("0.01"))
    vat = (net * VAT_MULTIPLIER).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    gross = net + vat
    return net, vat, gross


def fmt(amount):
    """Formatowanie kwoty po polsku: 1 000,00"""
    s = f"{amount:,.2f}"
    s = s.replace(",", " ").replace(".", ",")
    return s


def amount_words(amount):
    """Kwota słownie po polsku."""
    zlote = int(amount)
    grosze = round((amount - zlote) * 100)
    words = num2words(zlote, lang="pl")
    return f"{words} {grosze:02d}/100 PLN"


def format_bank_account(raw):
    """12345678901234567890123456 → 12 3456 7890 1234 5678 9012 3456"""
    s = raw.replace(" ", "")
    return f"{s[:2]} {s[2:6]} {s[6:10]} {s[10:14]} {s[14:18]} {s[18:22]} {s[22:26]}"


def last_day_of_month(d):
    """Ostatni dzień miesiąca — obsługuje lata przestępne."""
    _, last = calendar.monthrange(d.year, d.month)
    return d.replace(day=last)


def service_date(issue_date, end_of_month):
    """Data sprzedaży: ostatni dzień miesiąca lub data wystawienia."""
    if end_of_month:
        return last_day_of_month(issue_date)
    return issue_date


def full_invoice_number(code, dt):
    """A3 + 2026-03-25 → A3/3/2026"""
    return f"{code}/{dt.month}/{dt.year}"


def find_fonts():
    for regular, bold in FONT_PATHS:
        if Path(regular).exists() and Path(bold).exists():
            return regular, bold
    print("Błąd: nie znaleziono czcionek (Arial lub DejaVu Sans).")
    print("Umieść DejaVuSans.ttf i DejaVuSans-Bold.ttf w katalogu fonts/")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Description template
# ---------------------------------------------------------------------------

def render_description(template, issue_date):
    """Zamiana $M → English month, $Y → year. Bez znaczników = bez zmian."""
    if "$M" not in template and "$Y" not in template:
        return template
    result = template
    result = result.replace("$M", ENGLISH_MONTHS[issue_date.month])
    result = result.replace("$Y", str(issue_date.year))
    return result


# ---------------------------------------------------------------------------
# Invoice DB — autonumeracja
# ---------------------------------------------------------------------------

def load_invoice_db():
    if not INVOICE_DB.exists():
        return []
    with open(INVOICE_DB, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or []


def save_invoice_db(entries):
    with open(INVOICE_DB, "w", encoding="utf-8") as f:
        yaml.dump(entries, f, allow_unicode=True, default_flow_style=False)


def next_invoice_code(month, year):
    """Zwraca następny kod faktury (np. A4) dla danego miesiąca/roku."""
    entries = load_invoice_db()
    max_seq = 0
    suffix = f"/{month}/{year}"
    for entry in entries:
        num = entry.get("number", "")
        if num.endswith(suffix):
            # Wyciągnij sekwencyjny numer z "A3/5/2026" → 3
            code = num.split("/")[0]  # "A3"
            try:
                seq = int(code[1:])  # 3
                max_seq = max(max_seq, seq)
            except (ValueError, IndexError):
                pass
    return f"A{max_seq + 1}"


def record_invoice(inv_number, alias, issue_date, amount):
    """Zapisz fakturę do DB."""
    entries = load_invoice_db()
    entries.append({
        "number": inv_number,
        "alias": alias,
        "date": issue_date,
        "amount": float(amount),
        "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    })
    save_invoice_db(entries)


# ---------------------------------------------------------------------------
# XSD Validation
# ---------------------------------------------------------------------------

def validate_xsd(xml_path):
    """Walidacja XML przeciwko schematowi FA(3). Zwraca True jeśli valid."""
    xsd_dir = XSD_DIR
    if not (xsd_dir / "schemat.xsd").exists():
        print("UWAGA: Brak plików XSD w katalogu xsd/ — pomijam walidację.")
        return True

    class LocalResolver(etree.Resolver):
        def resolve(self, url, public_id, context):
            for f in xsd_dir.glob("*.xsd"):
                if url.endswith(f.name):
                    return self.resolve_filename(str(f), context)
            return None

    xsd_parser = etree.XMLParser()
    xsd_parser.resolvers.add(LocalResolver())
    xsd_doc = etree.parse(str(xsd_dir / "schemat.xsd"), xsd_parser)
    schema = etree.XMLSchema(xsd_doc)
    xml_doc = etree.parse(str(xml_path))

    if schema.validate(xml_doc):
        print("XSD: VALID")
        return True

    print("XSD: INVALID")
    for error in schema.error_log:
        print(f"  Linia {error.line}: {error.message}")
    return False


# ---------------------------------------------------------------------------
# PDF Generation
# ---------------------------------------------------------------------------

class InvoicePDF(FPDF):
    LIGHT_BLUE = (220, 240, 255)
    GREY = (120, 120, 120)

    def __init__(self, font_regular, font_bold):
        super().__init__()
        self.add_font("Invoice", "", font_regular)
        self.add_font("Invoice", "B", font_bold)
        self.set_auto_page_break(auto=True, margin=20)

    def _right_bilingual_header(self, pw, pl_label, en_label, value, pl_size, en_size):
        """Nagłówek: 'PL / EN: wartość' wyrównany do prawej."""
        # Zmierz wszystkie części
        self.set_font("Invoice", "B", pl_size)
        pl_text = f"{pl_label} "
        pl_w = self.get_string_width(pl_text)
        self.set_font("Invoice", "", en_size)
        en_text = f"/ {en_label}: "
        en_w = self.get_string_width(en_text)
        self.set_font("Invoice", "B", pl_size)
        val_w = self.get_string_width(value)
        total_w = pl_w + en_w + val_w
        h = pl_size * 0.6

        x = self.w - self.r_margin - total_w
        self.set_x(x)
        self.set_font("Invoice", "B", pl_size)
        self.cell(pl_w, h, pl_text, new_x="END")
        self.set_font("Invoice", "", en_size)
        self.set_text_color(*self.GREY)
        self.cell(en_w, h, en_text, new_x="END")
        self.set_text_color(0, 0, 0)
        self.set_font("Invoice", "B", pl_size)
        self.cell(val_w, h, value, new_x="LMARGIN", new_y="NEXT")

    def build(self, inv):
        self.add_page()
        self.set_font("Invoice", "", 10)
        pw = self.w - self.l_margin - self.r_margin

        # --- Nagłówek faktury ---
        # "Numer faktury / Invoice no.: A2/3/2026"
        self._right_bilingual_header(
            pw, "Numer faktury", "Invoice no.", inv["numer"], 14, 9
        )
        # Daty
        for pl_label, en_label, value in [
            ("Data wystawienia", "Issue date", inv["data_wystawienia"]),
            ("Data sprzedaży", "Sale date", inv["data_sprzedazy"]),
        ]:
            self._right_bilingual_header(pw, pl_label, en_label, value, 9, 7)

        self.ln(4)

        # --- Wystawca / Nabywca ---
        col_w = pw / 2
        y_start = self.get_y()

        # Wystawca
        self.set_font("Invoice", "B", 10)
        self.cell(0, 6, "Wystawca:", new_x="END")
        self.set_font("Invoice", "", 7)
        self.set_text_color(*self.GREY)
        self.cell(0, 6, "  / Seller", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.set_font("Invoice", "", 10)
        self.cell(col_w, 6, inv["sprzedawca"]["nazwa"], new_x="LMARGIN", new_y="NEXT")
        self.cell(col_w, 6, inv["sprzedawca"]["ulica"], new_x="LMARGIN", new_y="NEXT")
        self.cell(col_w, 6, f"{inv['sprzedawca']['kod_pocztowy']} {inv['sprzedawca']['miasto']}", new_x="LMARGIN", new_y="NEXT")
        self.cell(col_w, 6, f"NIP: {inv['sprzedawca']['nip']}", new_x="LMARGIN", new_y="NEXT")
        y_end_left = self.get_y()

        # Nabywca
        self.set_y(y_start)
        self.set_x(self.l_margin + col_w)
        self.set_font("Invoice", "B", 10)
        self.cell(col_w, 6, "Nabywca:", new_x="END")
        self.set_font("Invoice", "", 7)
        self.set_text_color(*self.GREY)
        self.cell(0, 6, "  / Buyer", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.set_x(self.l_margin + col_w)
        self.set_font("Invoice", "", 10)
        self.multi_cell(col_w, 6, inv["nabywca"]["nazwa"])
        self.set_x(self.l_margin + col_w)
        self.cell(col_w, 6, inv["nabywca"]["ulica"], new_x="LMARGIN", new_y="NEXT")
        self.set_x(self.l_margin + col_w)
        self.cell(col_w, 6, f"{inv['nabywca']['kod_pocztowy']} {inv['nabywca']['miasto']}", new_x="LMARGIN", new_y="NEXT")
        self.set_x(self.l_margin + col_w)
        self.cell(col_w, 6, f"NIP / Tax ID: {inv['nabywca']['nip']}", new_x="LMARGIN", new_y="NEXT")

        if inv.get("numer_zamowienia"):
            self.set_x(self.l_margin + col_w)
            self.set_font("Invoice", "B", 10)
            self.cell(col_w, 6, f"Nr zamówienia / PO: {inv['numer_zamowienia']}", new_x="LMARGIN", new_y="NEXT")
            self.set_font("Invoice", "", 10)

        y_end_right = self.get_y()
        self.set_y(max(y_end_left, y_end_right) + 4)

        # --- Tabela pozycji ---
        cols = [
            ("Lp.", "No.", 10, "C"),
            ("Pozycja", "Description", 38, "L"),
            ("PKWiU", "Code", 16, "C"),
            ("Cena netto", "Unit price", 22, "R"),
            ("Ilość", "Qty", 12, "C"),
            ("Jedn.", "Unit", 14, "C"),
            ("Wartość netto", "Net amount", 24, "R"),
            ("VAT%", "VAT%", 12, "C"),
            ("Kwota VAT", "VAT amount", 20, "R"),
            ("Wart. brutto", "Gross", 24, "R"),
        ]
        total_col = sum(c[2] for c in cols)
        scale = pw / total_col
        cols_scaled = [(pl, en, w * scale, a) for pl, en, w, a in cols]

        row_h = 7

        # Nagłówek tabeli — PL
        self.set_fill_color(*self.LIGHT_BLUE)
        self.set_font("Invoice", "B", 8)
        for pl, en, w, align in cols_scaled:
            self.cell(w, 5, pl, border="LTR", align=align, fill=True)
        self.ln()
        # Nagłówek tabeli — EN
        self.set_font("Invoice", "", 6)
        self.set_text_color(*self.GREY)
        for pl, en, w, align in cols_scaled:
            self.cell(w, 3, en, border="LBR", align=align, fill=True)
        self.ln()
        self.set_text_color(0, 0, 0)

        cols = [(pl, w, a) for pl, en, w, a in cols_scaled]

        # Wiersz danych — oblicz wysokość na podstawie opisu (wrap)
        self.set_font("Invoice", "", 9)
        desc_col_w = cols[1][1]
        desc_lines = self.multi_cell(
            desc_col_w, row_h, inv["opis"], border=0, dry_run=True, output="LINES"
        )
        data_row_h = max(row_h, row_h * len(desc_lines))

        values = [
            "1", inv["opis"], "", fmt(inv["netto"]),
            "1", "usługa", fmt(inv["netto"]),
            "23", fmt(inv["vat"]), fmt(inv["brutto"]),
        ]
        y_row = self.get_y()
        for i, ((_, w, align), val) in enumerate(zip(cols, values)):
            self.set_xy(self.l_margin + sum(c[1] for c in cols[:i]), y_row)
            if i == 1:  # kolumna Pozycja — multi_cell z wrapem
                self.multi_cell(w, row_h, val, border=1, align=align)
            else:
                self.cell(w, data_row_h, val, border=1, align=align)
        self.set_y(y_row + data_row_h)

        # Razem / Total
        self.set_font("Invoice", "B", 9)
        label_cols_w = sum(c[1] for c in cols[:6])
        val_cols = cols[6:]
        self.cell(label_cols_w, row_h, "Razem / Total", border=1, align="R")
        self.cell(val_cols[0][1], row_h, fmt(inv["netto"]), border=1, align="R")
        self.cell(val_cols[1][1], row_h, "- - -", border=1, align="C")
        self.cell(val_cols[2][1], row_h, fmt(inv["vat"]), border=1, align="R")
        self.cell(val_cols[3][1], row_h, fmt(inv["brutto"]), border=1, align="R")
        self.ln()

        # Rozliczenie VAT / VAT summary
        self.set_font("Invoice", "B", 9)
        self.cell(label_cols_w, row_h, "Rozliczenie VAT / VAT summary (PLN)", border=1, align="R", fill=True)
        self.set_font("Invoice", "", 9)
        self.cell(val_cols[0][1], row_h, fmt(inv["netto"]), border=1, align="R", fill=True)
        self.cell(val_cols[1][1], row_h, "23", border=1, align="C", fill=True)
        self.cell(val_cols[2][1], row_h, fmt(inv["vat"]), border=1, align="R", fill=True)
        self.cell(val_cols[3][1], row_h, fmt(inv["brutto"]), border=1, align="R", fill=True)
        self.ln(12)

        # --- Podsumowanie płatności ---
        label_w = 80
        val_w = pw - label_w

        def _row(pl_label, en_label, value, bold_val=False):
            self.set_font("Invoice", "B", 10)
            self.cell(label_w, 5, pl_label, align="R", new_x="LMARGIN", new_y="NEXT")
            self.set_font("Invoice", "", 7)
            self.set_text_color(*self.GREY)
            x_after_label = self.l_margin + label_w
            self.cell(label_w, 3, en_label, align="R")
            self.set_text_color(0, 0, 0)
            self.set_font("Invoice", "B" if bold_val else "", 10)
            self.set_xy(x_after_label, self.get_y() - 5)
            self.cell(val_w, 8, value, align="R", new_x="LMARGIN", new_y="NEXT")

        _row("Do zapłaty:", "Total due", f"{fmt(inv['brutto'])} PLN", bold_val=True)
        _row("Słownie:", "In words", amount_words(inv["brutto"]))
        _row("Sposób zapłaty:", "Payment method", "przelew / bank transfer")
        _row("Termin:", "Due date", str(inv["termin_platnosci"]))
        _row("Rachunek:", "Bank account", format_bank_account(inv["sprzedawca"]["rachunek_bankowy"]))
        _row("BIC/SWIFT:", "BIC/SWIFT", inv["sprzedawca"].get("swift", ""))

        if inv.get("numer_zamowienia"):
            _row("Nr zamówienia:", "PO number", inv["numer_zamowienia"])


# ---------------------------------------------------------------------------
# KSeF XML Generation (FA(3))
# ---------------------------------------------------------------------------

def generate_ksef_xml(inv):
    root = etree.Element("Faktura", nsmap=NSMAP)

    # Naglowek
    naglowek = etree.SubElement(root, "Naglowek")
    kod = etree.SubElement(naglowek, "KodFormularza")
    kod.text = "FA"
    kod.set("kodSystemowy", "FA (3)")
    kod.set("wersjaSchemy", "1-0E")
    etree.SubElement(naglowek, "WariantFormularza").text = "3"
    etree.SubElement(naglowek, "DataWytworzeniaFa").text = (
        datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    )
    etree.SubElement(naglowek, "SystemInfo").text = "InvoiceGenerator v1.0"

    # Podmiot1 — Sprzedawca
    podmiot1 = etree.SubElement(root, "Podmiot1")
    dane1 = etree.SubElement(podmiot1, "DaneIdentyfikacyjne")
    etree.SubElement(dane1, "NIP").text = inv["sprzedawca"]["nip"]
    etree.SubElement(dane1, "Nazwa").text = inv["sprzedawca"]["nazwa"]
    adres1 = etree.SubElement(podmiot1, "Adres")
    etree.SubElement(adres1, "KodKraju").text = "PL"
    etree.SubElement(adres1, "AdresL1").text = inv["sprzedawca"]["ulica"]
    etree.SubElement(adres1, "AdresL2").text = (
        f"{inv['sprzedawca']['kod_pocztowy']} {inv['sprzedawca']['miasto']}"
    )

    # Podmiot2 — Nabywca
    podmiot2 = etree.SubElement(root, "Podmiot2")
    dane2 = etree.SubElement(podmiot2, "DaneIdentyfikacyjne")
    etree.SubElement(dane2, "NIP").text = inv["nabywca"]["nip"]
    etree.SubElement(dane2, "Nazwa").text = inv["nabywca"]["nazwa"]
    adres2 = etree.SubElement(podmiot2, "Adres")
    etree.SubElement(adres2, "KodKraju").text = "PL"
    etree.SubElement(adres2, "AdresL1").text = inv["nabywca"]["ulica"]
    etree.SubElement(adres2, "AdresL2").text = (
        f"{inv['nabywca']['kod_pocztowy']} {inv['nabywca']['miasto']}"
    )
    etree.SubElement(podmiot2, "JST").text = str(inv["jst"])
    etree.SubElement(podmiot2, "GV").text = str(inv["gv"])

    # Fa — dane faktury
    fa = etree.SubElement(root, "Fa")
    etree.SubElement(fa, "KodWaluty").text = "PLN"
    etree.SubElement(fa, "P_1").text = inv["data_wystawienia"]
    if inv.get("miejsce_wystawienia"):
        etree.SubElement(fa, "P_1M").text = inv["miejsce_wystawienia"]
    etree.SubElement(fa, "P_2").text = inv["numer"]
    if inv["data_sprzedazy"] != inv["data_wystawienia"]:
        etree.SubElement(fa, "P_6").text = inv["data_sprzedazy"]
    etree.SubElement(fa, "P_13_1").text = str(inv["netto"])
    etree.SubElement(fa, "P_14_1").text = str(inv["vat"])
    etree.SubElement(fa, "P_15").text = str(inv["brutto"])

    # Adnotacje
    adnotacje = etree.SubElement(fa, "Adnotacje")
    etree.SubElement(adnotacje, "P_16").text = "2"
    etree.SubElement(adnotacje, "P_17").text = "2"
    etree.SubElement(adnotacje, "P_18").text = "2"
    etree.SubElement(adnotacje, "P_18A").text = "2"
    zwolnienie = etree.SubElement(adnotacje, "Zwolnienie")
    etree.SubElement(zwolnienie, "P_19N").text = "1"
    nowe_sr = etree.SubElement(adnotacje, "NoweSrodkiTransportu")
    etree.SubElement(nowe_sr, "P_22N").text = "1"
    etree.SubElement(adnotacje, "P_23").text = "2"
    pmarzy = etree.SubElement(adnotacje, "PMarzy")
    etree.SubElement(pmarzy, "P_PMarzyN").text = "1"

    etree.SubElement(fa, "RodzajFaktury").text = "VAT"

    # FaWiersz — pozycja
    wiersz = etree.SubElement(fa, "FaWiersz")
    etree.SubElement(wiersz, "NrWierszaFa").text = "1"
    etree.SubElement(wiersz, "P_7").text = inv["opis"]
    etree.SubElement(wiersz, "P_8A").text = "usługa"
    etree.SubElement(wiersz, "P_8B").text = "1"
    etree.SubElement(wiersz, "P_9A").text = str(inv["netto"])
    etree.SubElement(wiersz, "P_11").text = str(inv["netto"])
    etree.SubElement(wiersz, "P_12").text = "23"

    # Platnosc
    platnosc = etree.SubElement(fa, "Platnosc")
    termin_el = etree.SubElement(platnosc, "TerminPlatnosci")
    etree.SubElement(termin_el, "Termin").text = inv["termin_platnosci"]
    etree.SubElement(platnosc, "FormaPlatnosci").text = "6"
    rachunek = etree.SubElement(platnosc, "RachunekBankowy")
    etree.SubElement(rachunek, "NrRB").text = inv["sprzedawca"]["rachunek_bankowy"].replace(" ", "")
    swift = inv["sprzedawca"].get("swift")
    if swift:
        etree.SubElement(rachunek, "SWIFT").text = str(swift).strip().upper()

    # WarunkiTransakcji / Zamowienia
    if inv.get("numer_zamowienia"):
        warunki = etree.SubElement(fa, "WarunkiTransakcji")
        zamowienia = etree.SubElement(warunki, "Zamowienia")
        etree.SubElement(zamowienia, "NrZamowienia").text = inv["numer_zamowienia"]

    return root


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generowanie faktury VAT (PDF + KSeF XML)"
    )
    parser.add_argument("alias", help="Alias kontrahenta z contractors.yaml")
    parser.add_argument("-a", "--amount", default=None, help="Kwota netto PLN (domyślnie: z YAML)")
    parser.add_argument("-d", "--description", default=None, help="Opis pozycji (domyślnie: z YAML, template $M/$Y)")
    parser.add_argument("--date", default=None, help="Data wystawienia YYYY-MM-DD (domyślnie: 25. bieżącego miesiąca)")
    parser.add_argument("-n", "--number", default=None, help="Kod numeru faktury np. A3 (domyślnie: auto z invoices.yaml)")
    parser.add_argument("--po", default=None, help="Nadpisanie numeru zamówienia z YAML")
    parser.add_argument("--no-validate", action="store_true", help="Pomiń walidację XSD")
    args = parser.parse_args()

    # Ładowanie danych
    seller = load_seller()
    contractor = load_contractor(args.alias)

    # --- Data wystawienia ---
    if args.date:
        try:
            issue_date = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print("Błąd: data musi być w formacie YYYY-MM-DD")
            sys.exit(1)
    else:
        today = datetime.now()
        issue_date = today.replace(day=25)

    # --- Kwota ---
    amount = args.amount or contractor.get("default_amount")
    if not amount:
        print("Błąd: podaj kwotę (-a) lub ustaw default_amount w YAML kontrahenta.")
        sys.exit(1)

    # --- Opis ---
    desc_template = args.description or contractor.get("default_description")
    if not desc_template:
        print("Błąd: podaj opis (-d) lub ustaw default_description w YAML kontrahenta.")
        sys.exit(1)
    description = render_description(desc_template, issue_date)

    # --- Numer faktury ---
    if args.number:
        inv_code = args.number
    else:
        inv_code = next_invoice_code(issue_date.month, issue_date.year)

    net, vat, gross = calculate(amount)
    inv_number = full_invoice_number(inv_code, issue_date)

    payment_days = contractor.get("termin_platnosci", 14)
    payment_date = issue_date + timedelta(days=payment_days)

    po_number = args.po or contractor.get("numer_zamowienia")
    jst = str(contractor.get("jst", 2))
    gv = str(contractor.get("gv", 2))
    place_of_issue = (
        contractor.get("default_miejsce_wystawienia")
        or contractor.get("default_miejsce_sprzedazy")
        or seller.get("miasto")
    )
    if jst not in {"1", "2"}:
        print("Błąd: pole 'jst' w contractors.yaml musi mieć wartość 1 albo 2.")
        sys.exit(1)
    if gv not in {"1", "2"}:
        print("Błąd: pole 'gv' w contractors.yaml musi mieć wartość 1 albo 2.")
        sys.exit(1)

    eom = contractor.get("data_sprzedazy_koniec_miesiaca", False)
    svc_date = service_date(issue_date, eom)

    inv = {
        "numer": inv_number,
        "data_wystawienia": issue_date.strftime("%Y-%m-%d"),
        "data_sprzedazy": svc_date.strftime("%Y-%m-%d"),
        "sprzedawca": seller,
        "nabywca": contractor,
        "opis": description,
        "netto": net,
        "vat": vat,
        "brutto": gross,
        "termin_platnosci": payment_date.strftime("%Y-%m-%d"),
        "numer_zamowienia": po_number,
        "miejsce_wystawienia": place_of_issue,
        "jst": jst,
        "gv": gv,
    }

    OUTPUT_DIR.mkdir(exist_ok=True)
    safe_name = inv_number.replace("/", "_")

    # --- PDF ---
    font_regular, font_bold = find_fonts()
    pdf = InvoicePDF(font_regular, font_bold)
    pdf.build(inv)
    pdf_path = OUTPUT_DIR / f"{safe_name}.pdf"
    pdf.output(str(pdf_path))
    print(f"PDF: {pdf_path}")

    # --- KSeF XML ---
    xml_root = generate_ksef_xml(inv)
    xml_path = OUTPUT_DIR / f"{safe_name}.xml"
    tree = etree.ElementTree(xml_root)
    tree.write(str(xml_path), xml_declaration=True, encoding="UTF-8", pretty_print=True)
    print(f"XML: {xml_path}")

    # --- Walidacja XSD (domyślnie włączona) ---
    if not args.no_validate:
        if not validate_xsd(xml_path):
            sys.exit(1)

    # --- Zapis do DB ---
    record_invoice(inv_number, args.alias, issue_date.strftime("%Y-%m-%d"), net)

    print(f"\nFaktura {inv_number} wygenerowana pomyślnie.")
    print(f"  Opis: {description}")
    if po_number:
        print(f"  Nr zamówienia: {po_number}")


if __name__ == "__main__":
    main()
