# Generator faktur VAT + KSeF

Generowanie faktur VAT jako PDF i XML kompatybilny z KSeF (schemat FA(3)), z opcjonalna wysylka do systemu KSeF.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Struktura

```
config/
  seller.example.yaml      # przyklad (fikcyjne dane)
  contractors.example.yaml # przyklad (fikcyjne dane)
  seller.yaml              # lokalnie: dane sprzedawcy (ignorowany przez git)
  contractors.yaml         # lokalnie: kontrahenci (ignorowany przez git)
xsd/                   # schematy XSD FA(3) do walidacji offline
output/                # wygenerowane faktury (PDF, XML, UPO)
invoices.yaml          # baza wystawionych faktur (autonumeracja)
generate_invoice.py    # generowanie pojedynczej faktury (PDF + XML)
invoice_default.py     # batch: wystawianie faktur dla wielu aliasow
send_invoice.py        # wysylka XML do KSeF
```

## Konfiguracja

Najpierw utworz lokalne pliki konfiguracyjne na podstawie przykladow:

```bash
cp config/seller.example.yaml config/seller.yaml
cp config/contractors.example.yaml config/contractors.yaml
```

Nastepnie podmien dane na swoje. 

### Dane sprzedawcy (`config/seller.yaml`)

```yaml
nazwa: "EXAMPLE SOFTWARE SP. Z O.O."
nip: "9999999999"
ulica: "ul. Przykladowa 10/2"
kod_pocztowy: "00-001"
miasto: "Warszawa"
rachunek_bankowy: "11112222333344445555666677"
swift: "EXAMPLPWXXX"
```

### Kontrahenci (`config/contractors.yaml`)

Kazdy kontrahent ma unikalny alias. Dla tego samego klienta z roznymi PO — osobne wpisy:

```yaml
example-client:
  nazwa: "Example Client Sp. z o.o."
  nip: "1234567890"
  ulica: "ul. Testowa 1"
  kod_pocztowy: "00-100"
  miasto: "Krakow"
  termin_platnosci: 14
  numer_zamowienia: ~
  data_sprzedazy_koniec_miesiaca: false
  default_amount: 5000
  default_description: "Uslugi programistyczne"

example-client-po:
  nazwa: "Example Client Sp. z o.o."
  nip: "1234567890"
  ulica: "ul. Testowa 1"
  kod_pocztowy: "00-100"
  miasto: "Krakow"
  termin_platnosci: 14
  numer_zamowienia: "PO-EXAMPLE-2026-001"
  data_sprzedazy_koniec_miesiaca: true
  default_amount: 2500
  default_description: "DevSecOps services for $M $Y"
```

| Pole | Opis |
|---|---|
| `termin_platnosci` | Dni na zaplate (od daty wystawienia) |
| `numer_zamowienia` | Numer PO (`~` = brak) |
| `data_sprzedazy_koniec_miesiaca` | `true` = ostatni dzien miesiaca, `false` = data wystawienia |
| `default_amount` | Domyslna kwota netto (nadpisywana przez `-a`) |
| `default_description` | Domyslny opis (`$M` = miesiac po angielsku, `$Y` = rok) |

## Generowanie faktury

```bash
# Minimalna forma — wszystko z domyslnych wartosci
python3 generate_invoice.py example-client-po

# Nadpisanie wybranych wartosci
python3 generate_invoice.py example-client-po -a 3000 -d "Custom description" --date 2026-05-01 -n A3
```

| Argument | Opis | Domyslnie |
|---|---|---|
| `ALIAS` | Alias kontrahenta | (wymagany) |
| `-a, --amount` | Kwota netto PLN | `default_amount` z YAML |
| `-d, --description` | Opis pozycji | `default_description` z YAML (z $M/$Y) |
| `--date` | Data wystawienia (YYYY-MM-DD) | 25. biezacego miesiaca |
| `-n, --number` | Kod numeru (np. A3) | auto z `invoices.yaml` |
| `--po` | Nadpisanie numeru PO | `numer_zamowienia` z YAML |
| `--no-validate` | Pomin walidacje XSD | walidacja domyslnie wlaczona |

### Autonumeracja

Skrypt automatycznie numeruje faktury: A1/3/2026, A2/3/2026, A3/3/2026...
Nowy miesiac resetuje numeracje do A1. Historia zapisana w `invoices.yaml`.

### Template opisu ($M, $Y)

W `default_description` mozna uzyc znacznikow:
- `$M` — nazwa miesiaca po angielsku (np. March, May, December)
- `$Y` — rok (np. 2026)

Przyklad: `"DevSecOps services for $M $Y"` → `"DevSecOps services for March 2026"`

Bez znacznikow opis jest uzywany bez zmian.

## Wysylka do KSeF

```bash
# TEST (bez certyfikatu)
python3 send_invoice.py output/A1_3_2026.xml

# PRODUKCJA (wymaga certyfikatu)
python3 send_invoice.py output/A1_3_2026.xml --cert config/cert.crt --key config/cert.key --key-password "<KEY_PASSWORD>" --prod
```

| Argument | Opis |
|---|---|
| `xml` | Sciezka do pliku XML |
| `--cert` | Certyfikat (.crt) — wymagany dla `--prod` |
| `--key` | Klucz prywatny (.key) — wymagany dla `--prod` |
| `--key-password` | Haslo do klucza (jesli zaszyfrowany) |
| `--prod` | Wysylka na PRODUKCJE (domyslnie: TEST) |
| `--skip-validation` | Pominiecie walidacji XSD |

Na TEST certyfikat generuje sie automatycznie.

## Typowe flow

```bash
# Pojedyncza faktura
python3 generate_invoice.py example-client-po

# Wyslij na TEST
python3 send_invoice.py output/A1_3_2026.xml

# Jesli OK, wyslij na produkcje
python3 send_invoice.py output/A1_3_2026.xml --cert config/cert.crt --key config/cert.key --key-password "HASLO" --prod
```

## Certyfikaty KSeF

Srodowisko **TEST** nie wymaga certyfikatu.

Do **produkcji** potrzebujesz certyfikatu z MCU (Modul Certyfikatow i Uprawnien) na portalu KSeF.
