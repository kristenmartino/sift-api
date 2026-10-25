"""Small, cited reference overrides for legal terms found wrong in live copy."""
import re

SECTION_338_URL = "https://www.govinfo.gov/content/pkg/USCODE-2024-title19/pdf/USCODE-2024-title19-chap4-subtitleII-partII-sec1338.pdf"
SECTION_338_DEFINITION = (
    "Tariffs imposed under Section 338 of the Tariff Act of 1930 "
    "to counter foreign discrimination against U.S. commerce."
)


def reference_definition(term: str) -> str | None:
    if re.fullmatch(r"section\s+338(?:\s+tariffs)?", term.strip(), re.I):
        return SECTION_338_DEFINITION
    return None
