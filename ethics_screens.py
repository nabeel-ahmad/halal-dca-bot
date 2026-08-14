"""
Two mechanical screens, checked against tickers you already hold or that show
up as weekly candidates:

  1. BDS priority-boycott targets — a small, manually-curated list of
     tickers named on bdsmovement.net's boycott target list. This is NOT a
     large structured database (BDS doesn't publish one) — it's a short,
     narrative list of named companies that changes rarely. BDS_TARGETS
     below was compiled from https://bdsmovement.net/get-involved/what-to-boycott
     and needs periodic manual re-checking against that page, not automated
     scraping (the page is prose, not a table, and any ticker mapping needs
     a human to verify the company match is correct before trusting it).
     Some named targets are dropped here because their public ticker
     couldn't be confirmed with confidence (Siemens, Carrefour, AXA,
     Reebok) — a wrong ticker mapping would either falsely clear or falsely
     flag a real holding, both worse than just not covering it yet.

  2. Major US Department of Defense prime contractors — a short list of
     well-known top-20 DoD primes by contract value (Lockheed Martin,
     Boeing, RTX, etc.), sourced from public federal contract-spending data
     (USAspending.gov) and general knowledge of the defense sector. Not
     exhaustive — smaller/subcontracted defense revenue won't be caught.

Neither list is authoritative or complete. Both are reported as "named
source X lists this company" — not as Claude's own political judgment about
any company.
"""

TODAY_NOTE = "Lists last manually compiled 2026-08-14 — re-verify against bdsmovement.net periodically."

BDS_TARGETS = {
    "CVX": {"name": "Chevron", "reason": "Extracts natural gas Israel claims from contested maritime territory"},
    "INTC": {"name": "Intel", "reason": "Major manufacturing investment in Israel"},
    "DELL": {"name": "Dell Technologies", "reason": "Supplies servers/hardware to the Israeli military"},
    "HPQ": {"name": "HP Inc.", "reason": "Provides technology to Israeli military, prisons, and government"},
    "HPE": {"name": "Hewlett Packard Enterprise", "reason": "Provides technology to Israeli military, prisons, and government"},
    "MSFT": {"name": "Microsoft", "reason": "Azure cloud/AI services contracted by the Israeli military"},
    "DIS": {"name": "Disney", "reason": "BDS-listed re: Disney+ and Marvel content"},
    "RMAX": {"name": "RE/MAX Holdings", "reason": "Franchisees market property in Israeli settlements"},
    "GOOGL": {"name": "Alphabet (Google)", "reason": "Project Nimbus cloud/AI contract with the Israeli government"},
    "GOOG": {"name": "Alphabet (Google)", "reason": "Project Nimbus cloud/AI contract with the Israeli government"},
    "AMZN": {"name": "Amazon", "reason": "Project Nimbus cloud/AI contract with the Israeli government"},
    "BKNG": {"name": "Booking Holdings", "reason": "Lists rental properties in Israeli settlements"},
    "ABNB": {"name": "Airbnb", "reason": "Lists rental properties in Israeli settlements"},
    "EXPE": {"name": "Expedia Group", "reason": "Lists rental properties in Israeli settlements"},
    "TEVA": {"name": "Teva Pharmaceutical", "reason": "Israeli company named as a BDS target"},
    "WIX": {"name": "Wix.com", "reason": "Israeli company named as a BDS target"},
    "ESLT": {"name": "Elbit Systems", "reason": "Israeli military-security manufacturer"},
    "MCD": {"name": "McDonald's", "reason": "Grassroots BDS boycott target"},
    "KO": {"name": "Coca-Cola", "reason": "Grassroots BDS boycott target"},
    "QSR": {"name": "Restaurant Brands International (Burger King)", "reason": "Grassroots BDS boycott target"},
    "DPZ": {"name": "Domino's Pizza", "reason": "Grassroots BDS boycott target"},
    "PZZA": {"name": "Papa John's", "reason": "Grassroots BDS boycott target"},
    "YUM": {"name": "Yum! Brands (Pizza Hut)", "reason": "Grassroots BDS boycott target"},
}
BDS_SOURCE_URL = "https://bdsmovement.net/get-involved/what-to-boycott"

DOD_CONTRACTORS = {
    "LMT": "Lockheed Martin",
    "BA": "Boeing",
    "RTX": "RTX Corp (Raytheon)",
    "GD": "General Dynamics",
    "NOC": "Northrop Grumman",
    "LHX": "L3Harris Technologies",
    "LDOS": "Leidos",
    "BAH": "Booz Allen Hamilton",
    "HII": "Huntington Ingalls Industries",
    "TXT": "Textron",
    "KTOS": "Kratos Defense & Security Solutions",
    "AVAV": "AeroVironment",
    "CACI": "CACI International",
    "SAIC": "Science Applications International Corp",
    "TDY": "Teledyne Technologies",
}
DOD_SOURCE_NOTE = "Top-20 DoD prime contractors by contract value, per USAspending.gov federal award data"


def check_ethics_flags(ticker):
    """Returns a list of flag dicts for the given ticker — empty if none."""
    ticker = ticker.upper()
    flags = []
    if ticker in BDS_TARGETS:
        info = BDS_TARGETS[ticker]
        flags.append({
            "type": "BDS",
            "detail": f"{info['name']} — {info['reason']}",
            "source": BDS_SOURCE_URL,
        })
    if ticker in DOD_CONTRACTORS:
        flags.append({
            "type": "PENTAGON",
            "detail": f"{DOD_CONTRACTORS[ticker]} — major US DoD prime contractor",
            "source": DOD_SOURCE_NOTE,
        })
    return flags
