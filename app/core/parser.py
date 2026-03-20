
import re
import unicodedata
from typing import List, Optional, Tuple

# A simple type alias for clarity
# (quantity, card_name, set_code, collector_number, is_foil)
ParsedCard = Tuple[int, str, Optional[str], Optional[str], Optional[bool]]

# Regex to find the quantity and the rest of the line.
QTY_NAME_REGEX = re.compile(r"(\d+)\s+(.+)")
# Regex to find the set and collector number at a line end.
# It looks for a parenthesized set code (3-5 chars) followed by a collector number.
# The collector number can be alphanumeric and may include a 'p' prefix (for promos).
#
# NOTE: We intentionally restrict collector chars to ASCII [0-9A-Za-z] so symbols like
# Moxfield's foil star (★) don't get captured as part of the collector number.
SET_NUM_REGEX = re.compile(r'\s\((\w{3,5})\)\s+(p?[0-9A-Za-z]+)')


def parse_decklist(decklist: str) -> List[ParsedCard]:
    """
    Parses a multiline string representing a decklist into a list of cards.
    This function uses a robust procedural method instead of a single complex regex
    to handle complex card names with special characters.

    Args:
        decklist: A string containing the decklist, with one card entry per line.

    Returns:
        A list of tuples, where each tuple contains:
        (quantity, card_name, set_code, collector_number, is_foil).
        `set_code`, `collector_number`, and `is_foil` can be None if not provided.
    """
    parsed_cards: List[ParsedCard] = []
    
    # Normalize different newline characters to a standard \n
    normalized_decklist = decklist.replace('\r\n', '\n')

    for line in normalized_decklist.strip().splitlines():
        # --- Pre-processing ---
        line = line.strip()
        # Normalize smart quotes ( ‘ ’ ) to standard apostrophes ( ' )
        line = line.replace("’", "'").replace("‘", "'")
        # Normalize the DFC separator from user input " / " to the DB format " // "
        line = line.replace(" / ", " // ")
        
        # Skip empty lines or lines that are comments
        if not line or line.startswith('//'):
            continue

        # --- Parsing Logic ---
        card_name: str
        set_code: Optional[str] = None
        collector_number: Optional[str] = None
        is_foil: Optional[bool] = None
        
        name_part = line
        
        # 1. Try to find a set/collector number at the end of the line
        set_match = SET_NUM_REGEX.search(line)
        if set_match:
            set_code = set_match.group(1)
            collector_number = set_match.group(2).strip()
            # Detect foil markers from Moxfield-style output.
            # Example: "... (SLD) 1496★ *F*"
            remainder = line[set_match.end():]
            if "*F*" in remainder or "★" in remainder:
                is_foil = True
            else:
                # If set/collector matched but no foil marker is present,
                # treat it as non-foil for Moxfield pasted text.
                is_foil = False

            # Your DB encodes foil collector rows with a trailing '★' in `collector_number`
            # (e.g. "1492★" for a foil variant). Preserve that so we can match variants.
            if is_foil and "★" in remainder and not collector_number.endswith("★"):
                collector_number = f"{collector_number}★"
            # The name is everything before the set/number block
            name_part = line[:set_match.start()].strip()

        # 2. Parse the quantity and name from the remaining part
        name_match = QTY_NAME_REGEX.match(name_part)
        if name_match:
            quantity = int(name_match.group(1))
            card_name = name_match.group(2).strip()
            # Normalize Unicode to prevent lookup mismatches (e.g., NFC vs NFD)
            card_name = unicodedata.normalize('NFC', card_name)
            parsed_cards.append((quantity, card_name, set_code, collector_number, is_foil))
        else:
            # Line has a format we don't recognize (e.g., no quantity)
            print(f"Skipping unparsable line: {line}")

    return parsed_cards
