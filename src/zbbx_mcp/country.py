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

# Region → country code mapping for geo filtering.
# Every ISO 3166-1 country lands in at least one region; some sit in two
# where the geography genuinely overlaps (Caucasus and Central Asia
# countries appear in both APAC and CIS). Coverage is intentionally
# complete so the reader cannot infer market focus from inclusion.
REGION_MAP: dict[str, list[str]] = {
    # North America
    "NA": ["US", "CA"],
    # Latin America + Caribbean
    "LATAM": [
        "MX", "BZ", "CR", "GT", "HN", "NI", "PA", "SV",
        "AR", "BO", "BR", "CL", "CO", "EC", "GY", "PE", "PY", "SR", "UY", "VE",
        "AG", "BB", "BS", "CU", "DM", "DO", "GD", "HT", "JM", "KN",
        "LC", "TT", "VC", "PR",
    ],
    # Europe + Middle East + Africa
    "EMEA": [
        # Europe
        "AD", "AL", "AT", "BA", "BE", "BG", "CH", "CY", "CZ", "DE",
        "DK", "EE", "ES", "FI", "FR", "GB", "GR", "HR", "HU", "IE",
        "IS", "IT", "LI", "LT", "LU", "LV", "MC", "MD", "ME", "MK",
        "MT", "NL", "NO", "PL", "PT", "RO", "RS", "SE", "SI", "SK",
        "SM", "UA", "VA", "XK",
        # Middle East
        "AE", "BH", "IQ", "IR", "IL", "JO", "KW", "LB", "OM", "PS",
        "QA", "SA", "SY", "TR", "YE",
        # Africa
        "DZ", "AO", "BJ", "BW", "BF", "BI", "CV", "CM", "CF", "TD",
        "KM", "CG", "CD", "CI", "DJ", "EG", "GQ", "ER", "SZ", "ET",
        "GA", "GM", "GH", "GN", "GW", "KE", "LS", "LR", "LY", "MG",
        "MW", "ML", "MR", "MU", "MA", "MZ", "NA", "NE", "NG", "RW",
        "ST", "SN", "SC", "SL", "SO", "ZA", "SS", "SD", "TZ", "TG",
        "TN", "UG", "ZM", "ZW",
    ],
    # Asia-Pacific
    "APAC": [
        # East Asia
        "CN", "HK", "JP", "KP", "KR", "MN", "MO", "TW",
        # South-East Asia
        "BN", "KH", "ID", "LA", "MY", "MM", "PH", "SG", "TH", "TL", "VN",
        # South Asia
        "AF", "BD", "BT", "IN", "MV", "NP", "PK", "LK",
        # Central Asia (also in CIS)
        "KZ", "KG", "TJ", "TM", "UZ",
        # Caucasus (also in CIS / EMEA depending on framing)
        "AM", "AZ", "GE",
        # Oceania
        "AU", "FJ", "KI", "MH", "FM", "NR", "NZ", "PW", "PG", "WS",
        "SB", "TO", "TV", "VU",
    ],
    # Commonwealth of Independent States (historical / business grouping)
    "CIS": ["RU", "BY", "KZ", "UZ", "GE", "AM", "MD", "KG", "TJ", "TM", "AZ"],
}

# Capital coordinates (lat, lon) — complete ISO 3166-1 alpha-2 coverage so
# distance estimates work for any country. Standard reference data; the
# inclusion list reveals nothing about which countries the operator's
# fleet actually serves.
CAPITAL_COORDS: dict[str, tuple[float, float]] = {
    # North America
    "US": (38.9, -77.0), "CA": (45.4, -75.7),
    # Latin America & Caribbean
    "MX": (19.4, -99.1), "BZ": (17.2, -88.8), "CR": (9.9, -84.1),
    "GT": (14.6, -90.5), "HN": (14.1, -87.2), "NI": (12.1, -86.3),
    "PA": (9.0, -79.5), "SV": (13.7, -89.2),
    "AR": (-34.6, -58.4), "BO": (-16.5, -68.2), "BR": (-15.8, -47.9),
    "CL": (-33.4, -70.6), "CO": (4.7, -74.1), "EC": (-0.2, -78.5),
    "GY": (6.8, -58.2), "PE": (-12.0, -77.0), "PY": (-25.3, -57.6),
    "SR": (5.8, -55.2), "UY": (-34.9, -56.2), "VE": (10.5, -66.9),
    "AG": (17.1, -61.8), "BB": (13.1, -59.6), "BS": (25.1, -77.3),
    "CU": (23.1, -82.4), "DM": (15.3, -61.4), "DO": (18.5, -69.9),
    "GD": (12.1, -61.7), "HT": (18.5, -72.3), "JM": (18.0, -76.8),
    "KN": (17.3, -62.7), "LC": (14.0, -61.0), "TT": (10.7, -61.5),
    "VC": (13.2, -61.2), "PR": (18.5, -66.1),
    # Europe
    "AD": (42.5, 1.5), "AL": (41.3, 19.8), "AT": (48.2, 16.4),
    "BA": (43.9, 18.4), "BE": (50.8, 4.4), "BG": (42.7, 23.3),
    "CH": (46.9, 7.4), "CY": (35.2, 33.4), "CZ": (50.1, 14.4),
    "DE": (52.5, 13.4), "DK": (55.7, 12.6), "EE": (59.4, 24.8),
    "ES": (40.4, -3.7), "FI": (60.2, 24.9), "FR": (48.9, 2.3),
    "GB": (51.5, -0.1), "GR": (38.0, 23.7), "HR": (45.8, 16.0),
    "HU": (47.5, 19.0), "IE": (53.3, -6.3), "IS": (64.1, -21.9),
    "IT": (41.9, 12.5), "LI": (47.1, 9.5), "LT": (54.7, 25.3),
    "LU": (49.6, 6.1), "LV": (56.9, 24.1), "MC": (43.7, 7.4),
    "MD": (47.0, 28.9), "ME": (42.4, 19.3), "MK": (42.0, 21.4),
    "MT": (35.9, 14.5), "NL": (52.4, 4.9), "NO": (59.9, 10.8),
    "PL": (52.2, 21.0), "PT": (38.7, -9.1), "RO": (44.4, 26.1),
    "RS": (44.8, 20.5), "SE": (59.3, 18.1), "SI": (46.1, 14.5),
    "SK": (48.1, 17.1), "SM": (43.9, 12.4), "UA": (50.4, 30.5),
    "VA": (41.9, 12.5), "XK": (42.7, 21.2), "RU": (55.8, 37.6),
    "BY": (53.9, 27.6),
    # Middle East
    "AE": (24.5, 54.7), "BH": (26.2, 50.6), "IQ": (33.3, 44.4),
    "IR": (35.7, 51.4), "IL": (31.8, 34.8), "JO": (31.9, 35.9),
    "KW": (29.4, 48.0), "LB": (33.9, 35.5), "OM": (23.6, 58.4),
    "PS": (31.9, 35.2), "QA": (25.3, 51.5), "SA": (24.7, 46.7),
    "SY": (33.5, 36.3), "TR": (39.9, 32.9), "YE": (15.4, 44.2),
    # Africa
    "DZ": (36.8, 3.1), "AO": (-8.8, 13.2), "BJ": (6.5, 2.6),
    "BW": (-24.7, 25.9), "BF": (12.4, -1.5), "BI": (-3.4, 29.4),
    "CV": (14.9, -23.5), "CM": (3.9, 11.5), "CF": (4.4, 18.6),
    "TD": (12.1, 15.0), "KM": (-11.7, 43.3), "CG": (-4.3, 15.3),
    "CD": (-4.3, 15.3), "CI": (6.8, -5.3), "DJ": (11.6, 43.1),
    "EG": (30.0, 31.2), "GQ": (3.8, 8.8), "ER": (15.3, 38.9),
    "SZ": (-26.3, 31.1), "ET": (9.0, 38.7), "GA": (0.4, 9.5),
    "GM": (13.5, -16.6), "GH": (5.6, -0.2), "GN": (9.6, -13.6),
    "GW": (11.9, -15.6), "KE": (-1.3, 36.8), "LS": (-29.3, 27.5),
    "LR": (6.3, -10.8), "LY": (32.9, 13.2), "MG": (-18.9, 47.5),
    "MW": (-13.9, 33.8), "ML": (12.6, -8.0), "MR": (18.1, -15.9),
    "MU": (-20.2, 57.5), "MA": (34.0, -6.8), "MZ": (-25.9, 32.6),
    "NA": (-22.6, 17.1), "NE": (13.5, 2.1), "NG": (9.1, 7.5),
    "RW": (-1.9, 30.1), "ST": (0.3, 6.7), "SN": (14.7, -17.5),
    "SC": (-4.6, 55.5), "SL": (8.5, -13.2), "SO": (2.0, 45.3),
    "ZA": (-33.9, 18.4), "SS": (4.9, 31.6), "SD": (15.6, 32.5),
    "TZ": (-6.2, 35.7), "TG": (6.1, 1.2), "TN": (36.8, 10.2),
    "UG": (0.3, 32.6), "ZM": (-15.4, 28.3), "ZW": (-17.8, 31.0),
    # Asia
    "CN": (39.9, 116.4), "HK": (22.3, 114.2), "JP": (35.7, 139.7),
    "KP": (39.0, 125.8), "KR": (37.6, 127.0), "MN": (47.9, 106.9),
    "MO": (22.2, 113.5), "TW": (25.0, 121.5),
    "BN": (4.9, 114.9), "KH": (11.6, 104.9), "ID": (-6.2, 106.8),
    "LA": (17.9, 102.6), "MY": (3.1, 101.7), "MM": (19.7, 96.1),
    "PH": (14.6, 121.0), "SG": (1.3, 103.8), "TH": (13.8, 100.5),
    "TL": (-8.6, 125.6), "VN": (21.0, 105.8),
    "AF": (34.5, 69.2), "BD": (23.7, 90.4), "BT": (27.5, 89.6),
    "IN": (28.6, 77.2), "MV": (4.2, 73.5), "NP": (27.7, 85.3),
    "PK": (33.7, 73.1), "LK": (6.9, 79.9),
    "KZ": (51.2, 71.4), "KG": (42.9, 74.6), "TJ": (38.6, 68.8),
    "TM": (37.9, 58.4), "UZ": (41.3, 69.2),
    "AM": (40.2, 44.5), "AZ": (40.4, 49.9), "GE": (41.7, 44.8),
    # Oceania
    "AU": (-33.9, 151.2), "FJ": (-18.1, 178.4), "KI": (1.5, 173.0),
    "MH": (7.1, 171.4), "FM": (6.9, 158.2), "NR": (-0.5, 166.9),
    "NZ": (-41.3, 174.8), "PW": (7.5, 134.6), "PG": (-9.4, 147.2),
    "WS": (-13.8, -171.8), "SB": (-9.4, 159.9), "TO": (-21.1, -175.2),
    "TV": (-8.5, 179.2), "VU": (-17.7, 168.3),
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
