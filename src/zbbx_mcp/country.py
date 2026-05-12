"""Country code resolution: ISO-2/ISO-3/name normalisation, region maps.

Extracted from ``data.py`` so the country-specific reference data
(~250 entries) lives next to the helpers that consume it instead of
mixing with unrelated Zabbix constants. ``data.py`` re-exports the
public symbols from this module so existing ``from zbbx_mcp.data
import extract_country`` callers keep working.

Reference tables:
- ``REGION_MAP`` — continent → list of ISO-2 codes for geo filtering
- ``CAPITAL_COORDS`` — ISO-2 → (lat, lon) for distance estimation
- ``_COUNTRY_NAMES`` — ISO-3 + English names → ISO-2 (~200 entries,
  standard ISO 3166-1 reference data, ASCII-only)
- ``_COUNTRY_ALIASES`` — non-ISO codes to canonical form (UK → GB)

The reference data is intentionally the complete standard list rather
than a curated subset; the dict tells the reader nothing about which
countries the operator's fleet actually serves.
"""

from __future__ import annotations

import re

__all__ = [
    "REGION_MAP",
    "CAPITAL_COORDS",
    "extract_country",
    "normalize_country",
    "resolve_country",
    "countries_for_region",
]


_COUNTRY_RE = re.compile(
    r"(?:[-_]([a-z]{2})\d)"       # nl0105, de0267
    r"|(?:[-_]([a-z]{2})[-_])",   # -in-lite, -us-lite
    re.IGNORECASE,
)

# Region → country code mapping for geo filtering
REGION_MAP: dict[str, list[str]] = {
    "LATAM": ["AR", "BR", "MX", "CL", "CO", "PE", "VE", "EC", "UY", "PY", "BO", "CR", "PA"],
    "APAC": ["JP", "IN", "ID", "TH", "KZ", "AZ", "SG", "KR", "AU", "NZ", "PH", "VN", "MY", "TW", "HK"],
    "EMEA": ["NL", "DE", "FR", "GB", "ES", "IT", "SE", "FI", "NO", "DK", "PL", "CZ", "AT", "CH", "BE",
             "PT", "IE", "RO", "BG", "HR", "UA", "TR", "IL", "AE", "ZA", "NG", "EG", "KE"],
    "NA": ["US", "CA"],
    "CIS": ["RU", "BY", "KZ", "UZ", "GE", "AM", "MD"],
}

# Capital coordinates for distance estimation (lat, lon)
CAPITAL_COORDS: dict[str, tuple[float, float]] = {
    "AR": (-34.6, -58.4), "BR": (-15.8, -47.9), "MX": (19.4, -99.1),
    "CL": (-33.4, -70.6), "CO": (4.7, -74.1), "PE": (-12.0, -77.0),
    "VE": (10.5, -66.9), "EC": (-0.2, -78.5), "UY": (-34.9, -56.2),
    "US": (38.9, -77.0), "CA": (45.4, -75.7),
    "NL": (52.4, 4.9), "DE": (52.5, 13.4), "FR": (48.9, 2.3),
    "GB": (51.5, -0.1), "ES": (40.4, -3.7), "IT": (41.9, 12.5),
    "SE": (59.3, 18.1), "FI": (60.2, 24.9), "NO": (59.9, 10.8),
    "PL": (52.2, 21.0), "CZ": (50.1, 14.4), "AT": (48.2, 16.4),
    "CH": (46.9, 7.4), "BE": (50.8, 4.4), "PT": (38.7, -9.1),
    "IE": (53.3, -6.3), "RO": (44.4, 26.1), "BG": (42.7, 23.3),
    "HR": (45.8, 16.0), "UA": (50.4, 30.5), "TR": (39.9, 32.9),
    "IL": (31.8, 34.8), "AE": (24.5, 54.7), "ZA": (-33.9, 18.4),
    "JP": (35.7, 139.7), "IN": (28.6, 77.2), "ID": (-6.2, 106.8),
    "TH": (13.8, 100.5), "KZ": (51.2, 71.4), "AZ": (40.4, 49.9),
    "SG": (1.3, 103.8), "KR": (37.6, 127.0), "AU": (-33.9, 151.2),
    "RU": (55.8, 37.6), "BY": (53.9, 27.6),
    "DK": (55.7, 12.6), "HK": (22.3, 114.2), "TW": (25.0, 121.5),
}


_COUNTRY_ALIASES = {"UK": "GB"}  # normalize non-ISO codes


# Country-name and ISO-3 → ISO-2 lookup for normalize_country().
# Standard ISO 3166-1 reference data; built so callers can pass either
# "RU", "RUS", or "Russia" and get back a canonical "RU".
# Keys are uppercased so the lookup is case-insensitive.
_COUNTRY_NAMES: dict[str, str] = {
    # Africa
    "DZA": "DZ", "ALGERIA": "DZ",
    "AGO": "AO", "ANGOLA": "AO",
    "BEN": "BJ", "BENIN": "BJ",
    "BWA": "BW", "BOTSWANA": "BW",
    "BFA": "BF", "BURKINA FASO": "BF",
    "BDI": "BI", "BURUNDI": "BI",
    "CMR": "CM", "CAMEROON": "CM",
    "CPV": "CV", "CABO VERDE": "CV", "CAPE VERDE": "CV",
    "CAF": "CF", "CENTRAL AFRICAN REPUBLIC": "CF",
    "TCD": "TD", "CHAD": "TD",
    "COM": "KM", "COMOROS": "KM",
    "COG": "CG", "CONGO": "CG", "REPUBLIC OF THE CONGO": "CG",
    "COD": "CD", "DEMOCRATIC REPUBLIC OF THE CONGO": "CD", "DR CONGO": "CD",
    "CIV": "CI", "COTE D'IVOIRE": "CI", "IVORY COAST": "CI",
    "DJI": "DJ", "DJIBOUTI": "DJ",
    "EGY": "EG", "EGYPT": "EG",
    "GNQ": "GQ", "EQUATORIAL GUINEA": "GQ",
    "ERI": "ER", "ERITREA": "ER",
    "SWZ": "SZ", "ESWATINI": "SZ", "SWAZILAND": "SZ",
    "ETH": "ET", "ETHIOPIA": "ET",
    "GAB": "GA", "GABON": "GA",
    "GMB": "GM", "GAMBIA": "GM",
    "GHA": "GH", "GHANA": "GH",
    "GIN": "GN", "GUINEA": "GN",
    "GNB": "GW", "GUINEA-BISSAU": "GW",
    "KEN": "KE", "KENYA": "KE",
    "LSO": "LS", "LESOTHO": "LS",
    "LBR": "LR", "LIBERIA": "LR",
    "LBY": "LY", "LIBYA": "LY",
    "MDG": "MG", "MADAGASCAR": "MG",
    "MWI": "MW", "MALAWI": "MW",
    "MLI": "ML", "MALI": "ML",
    "MRT": "MR", "MAURITANIA": "MR",
    "MUS": "MU", "MAURITIUS": "MU",
    "MAR": "MA", "MOROCCO": "MA",
    "MOZ": "MZ", "MOZAMBIQUE": "MZ",
    "NAM": "NA", "NAMIBIA": "NA",
    "NER": "NE", "NIGER": "NE",
    "NGA": "NG", "NIGERIA": "NG",
    "RWA": "RW", "RWANDA": "RW",
    "STP": "ST", "SAO TOME AND PRINCIPE": "ST",
    "SEN": "SN", "SENEGAL": "SN",
    "SYC": "SC", "SEYCHELLES": "SC",
    "SLE": "SL", "SIERRA LEONE": "SL",
    "SOM": "SO", "SOMALIA": "SO",
    "ZAF": "ZA", "SOUTH AFRICA": "ZA",
    "SSD": "SS", "SOUTH SUDAN": "SS",
    "SDN": "SD", "SUDAN": "SD",
    "TZA": "TZ", "TANZANIA": "TZ",
    "TGO": "TG", "TOGO": "TG",
    "TUN": "TN", "TUNISIA": "TN",
    "UGA": "UG", "UGANDA": "UG",
    "ZMB": "ZM", "ZAMBIA": "ZM",
    "ZWE": "ZW", "ZIMBABWE": "ZW",
    # Americas
    "ATG": "AG", "ANTIGUA AND BARBUDA": "AG",
    "ARG": "AR", "ARGENTINA": "AR",
    "BHS": "BS", "BAHAMAS": "BS",
    "BRB": "BB", "BARBADOS": "BB",
    "BLZ": "BZ", "BELIZE": "BZ",
    "BOL": "BO", "BOLIVIA": "BO",
    "BRA": "BR", "BRAZIL": "BR",
    "CAN": "CA", "CANADA": "CA",
    "CHL": "CL", "CHILE": "CL",
    "COL": "CO", "COLOMBIA": "CO",
    "CRI": "CR", "COSTA RICA": "CR",
    "CUB": "CU", "CUBA": "CU",
    "DMA": "DM", "DOMINICA": "DM",
    "DOM": "DO", "DOMINICAN REPUBLIC": "DO",
    "ECU": "EC", "ECUADOR": "EC",
    "SLV": "SV", "EL SALVADOR": "SV",
    "GRD": "GD", "GRENADA": "GD",
    "GTM": "GT", "GUATEMALA": "GT",
    "GUY": "GY", "GUYANA": "GY",
    "HTI": "HT", "HAITI": "HT",
    "HND": "HN", "HONDURAS": "HN",
    "JAM": "JM", "JAMAICA": "JM",
    "MEX": "MX", "MEXICO": "MX",
    "NIC": "NI", "NICARAGUA": "NI",
    "PAN": "PA", "PANAMA": "PA",
    "PRY": "PY", "PARAGUAY": "PY",
    "PER": "PE", "PERU": "PE",
    "PRI": "PR", "PUERTO RICO": "PR",
    "KNA": "KN", "SAINT KITTS AND NEVIS": "KN",
    "LCA": "LC", "SAINT LUCIA": "LC",
    "VCT": "VC", "SAINT VINCENT AND THE GRENADINES": "VC",
    "SUR": "SR", "SURINAME": "SR",
    "TTO": "TT", "TRINIDAD AND TOBAGO": "TT",
    "USA": "US", "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US",
    "URY": "UY", "URUGUAY": "UY",
    "VEN": "VE", "VENEZUELA": "VE",
    # Asia
    "AFG": "AF", "AFGHANISTAN": "AF",
    "ARM": "AM", "ARMENIA": "AM",
    "AZE": "AZ", "AZERBAIJAN": "AZ",
    "BHR": "BH", "BAHRAIN": "BH",
    "BGD": "BD", "BANGLADESH": "BD",
    "BTN": "BT", "BHUTAN": "BT",
    "BRN": "BN", "BRUNEI": "BN", "BRUNEI DARUSSALAM": "BN",
    "KHM": "KH", "CAMBODIA": "KH",
    "CHN": "CN", "CHINA": "CN",
    "GEO": "GE", "GEORGIA": "GE",
    "HKG": "HK", "HONG KONG": "HK",
    "IND": "IN", "INDIA": "IN",
    "IDN": "ID", "INDONESIA": "ID",
    "IRN": "IR", "IRAN": "IR",
    "IRQ": "IQ", "IRAQ": "IQ",
    "ISR": "IL", "ISRAEL": "IL",
    "JPN": "JP", "JAPAN": "JP",
    "JOR": "JO", "JORDAN": "JO",
    "KAZ": "KZ", "KAZAKHSTAN": "KZ",
    "KWT": "KW", "KUWAIT": "KW",
    "KGZ": "KG", "KYRGYZSTAN": "KG",
    "LAO": "LA", "LAOS": "LA",
    "LBN": "LB", "LEBANON": "LB",
    "MAC": "MO", "MACAO": "MO", "MACAU": "MO",
    "MYS": "MY", "MALAYSIA": "MY",
    "MDV": "MV", "MALDIVES": "MV",
    "MNG": "MN", "MONGOLIA": "MN",
    "MMR": "MM", "MYANMAR": "MM", "BURMA": "MM",
    "NPL": "NP", "NEPAL": "NP",
    "PRK": "KP", "NORTH KOREA": "KP",
    "OMN": "OM", "OMAN": "OM",
    "PAK": "PK", "PAKISTAN": "PK",
    "PSE": "PS", "PALESTINE": "PS",
    "PHL": "PH", "PHILIPPINES": "PH",
    "QAT": "QA", "QATAR": "QA",
    "SAU": "SA", "SAUDI ARABIA": "SA",
    "SGP": "SG", "SINGAPORE": "SG",
    "KOR": "KR", "SOUTH KOREA": "KR", "REPUBLIC OF KOREA": "KR",
    "LKA": "LK", "SRI LANKA": "LK",
    "SYR": "SY", "SYRIA": "SY",
    "TWN": "TW", "TAIWAN": "TW",
    "TJK": "TJ", "TAJIKISTAN": "TJ",
    "THA": "TH", "THAILAND": "TH",
    "TLS": "TL", "TIMOR-LESTE": "TL", "EAST TIMOR": "TL",
    "TUR": "TR", "TURKEY": "TR", "TURKIYE": "TR",
    "TKM": "TM", "TURKMENISTAN": "TM",
    "ARE": "AE", "UNITED ARAB EMIRATES": "AE", "UAE": "AE",
    "UZB": "UZ", "UZBEKISTAN": "UZ",
    "VNM": "VN", "VIETNAM": "VN",
    "YEM": "YE", "YEMEN": "YE",
    # Europe
    "ALB": "AL", "ALBANIA": "AL",
    "AND": "AD", "ANDORRA": "AD",
    "AUT": "AT", "AUSTRIA": "AT",
    "BLR": "BY", "BELARUS": "BY",
    "BEL": "BE", "BELGIUM": "BE",
    "BIH": "BA", "BOSNIA AND HERZEGOVINA": "BA",
    "BGR": "BG", "BULGARIA": "BG",
    "HRV": "HR", "CROATIA": "HR",
    "CYP": "CY", "CYPRUS": "CY",
    "CZE": "CZ", "CZECHIA": "CZ", "CZECH REPUBLIC": "CZ",
    "DNK": "DK", "DENMARK": "DK",
    "EST": "EE", "ESTONIA": "EE",
    "FIN": "FI", "FINLAND": "FI",
    "FRA": "FR", "FRANCE": "FR",
    "DEU": "DE", "GERMANY": "DE",
    "GRC": "GR", "GREECE": "GR",
    "HUN": "HU", "HUNGARY": "HU",
    "ISL": "IS", "ICELAND": "IS",
    "IRL": "IE", "IRELAND": "IE",
    "ITA": "IT", "ITALY": "IT",
    "XKX": "XK", "KOSOVO": "XK",
    "LVA": "LV", "LATVIA": "LV",
    "LIE": "LI", "LIECHTENSTEIN": "LI",
    "LTU": "LT", "LITHUANIA": "LT",
    "LUX": "LU", "LUXEMBOURG": "LU",
    "MLT": "MT", "MALTA": "MT",
    "MDA": "MD", "MOLDOVA": "MD",
    "MCO": "MC", "MONACO": "MC",
    "MNE": "ME", "MONTENEGRO": "ME",
    "NLD": "NL", "NETHERLANDS": "NL", "HOLLAND": "NL",
    "MKD": "MK", "NORTH MACEDONIA": "MK",
    "NOR": "NO", "NORWAY": "NO",
    "POL": "PL", "POLAND": "PL",
    "PRT": "PT", "PORTUGAL": "PT",
    "ROU": "RO", "ROMANIA": "RO",
    "RUS": "RU", "RUSSIA": "RU", "RUSSIAN FEDERATION": "RU",
    "SMR": "SM", "SAN MARINO": "SM",
    "SRB": "RS", "SERBIA": "RS",
    "SVK": "SK", "SLOVAKIA": "SK",
    "SVN": "SI", "SLOVENIA": "SI",
    "ESP": "ES", "SPAIN": "ES",
    "SWE": "SE", "SWEDEN": "SE",
    "CHE": "CH", "SWITZERLAND": "CH",
    "UKR": "UA", "UKRAINE": "UA",
    "GBR": "GB", "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB", "BRITAIN": "GB",
    "VAT": "VA", "VATICAN": "VA", "VATICAN CITY": "VA", "HOLY SEE": "VA",
    # Oceania
    "AUS": "AU", "AUSTRALIA": "AU",
    "FJI": "FJ", "FIJI": "FJ",
    "KIR": "KI", "KIRIBATI": "KI",
    "MHL": "MH", "MARSHALL ISLANDS": "MH",
    "FSM": "FM", "MICRONESIA": "FM",
    "NRU": "NR", "NAURU": "NR",
    "NZL": "NZ", "NEW ZEALAND": "NZ",
    "PLW": "PW", "PALAU": "PW",
    "PNG": "PG", "PAPUA NEW GUINEA": "PG",
    "WSM": "WS", "SAMOA": "WS",
    "SLB": "SB", "SOLOMON ISLANDS": "SB",
    "TON": "TO", "TONGA": "TO",
    "TUV": "TV", "TUVALU": "TV",
    "VUT": "VU", "VANUATU": "VU",
}


def countries_for_region(region: str) -> set[str]:
    """Return set of country codes for a region name. ALL returns everything."""
    r = region.upper()
    if r == "ALL":
        return {cc for codes in REGION_MAP.values() for cc in codes}
    return set(REGION_MAP.get(r, []))


def extract_country(hostname: str) -> str:
    """Extract 2-letter country code from hostname.

    Handles names with the embedded country pattern (e.g. ``srv-nl0105`` →
    ``NL``, ``srv-us01-lite`` → ``US``). Normalises ``UK`` to ``GB``.
    """
    m = _COUNTRY_RE.search(hostname)
    if not m:
        return ""
    cc = (m.group(1) or m.group(2) or "").upper()
    return _COUNTRY_ALIASES.get(cc, cc)


def normalize_country(value: str) -> str:
    """Normalize a country input to its ISO 3166-1 alpha-2 code.

    Accepts ISO-2, ISO-3, or English country name (case-insensitive).
    Applies the same ``UK → GB`` alias as ``extract_country``. Returns
    ``""`` for empty or unrecognised input so callers can decide on
    fallback or error message.
    """
    if not value:
        return ""
    key = value.strip().upper()
    if not key:
        return ""
    if len(key) == 2 and key.isalpha():
        return _COUNTRY_ALIASES.get(key, key)
    return _COUNTRY_NAMES.get(key, "")


def resolve_country(host: dict) -> str:
    """Resolve a host's country from name first, then Zabbix inventory.

    Order: ``extract_country(host['host'])`` → ``inventory.country_code``
    → ``normalize_country(inventory.country_name)``. Returns ``""`` when
    none of the three sources yield a valid 2-letter code.

    The caller must include ``selectInventory`` in its ``host.get`` to
    populate the inventory fields. Use this only inside country-filter
    branches — ``extract_country`` remains the source of truth for
    "what does the host name claim".
    """
    cc = extract_country(host.get("host", ""))
    if cc:
        return cc
    inv = host.get("inventory") or {}
    if isinstance(inv, dict):
        code = (inv.get("country_code") or "").strip()
        if code:
            normalized = normalize_country(code)
            if normalized:
                return normalized
        name = (inv.get("country_name") or "").strip()
        if name:
            normalized = normalize_country(name)
            if normalized:
                return normalized
    return ""
