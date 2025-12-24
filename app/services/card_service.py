
import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# --- Configuration ---
DATABASE_PATH = Path("app/data/cards.db")
SCRYFALL_API_URL = "https://api.scryfall.com"
# Rate limit for the fallback Scryfall client
SCRYFALL_REQUEST_DELAY = 0.1  # 100ms

# --- Logging ---
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

# --- Type Hint for a Card Record ---
# Using a Dict for easy conversion to/from JSON and database rows
CardData = Dict[str, Any]

# --- Scryfall API Fallback Client ---
# Use an async client for modern FastAPI integration
scryfall_client = httpx.AsyncClient(
    base_url=SCRYFALL_API_URL,
    timeout=10.0,
    headers={"User-Agent": "BlingMyDeck/1.0 (Python/httpx)"}
)

def _dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> Dict:
    """Factory to return sqlite results as dictionaries."""
    fields = [column[0] for column in cursor.description]
    return {key: value for key, value in zip(fields, row)}

def get_db_connection() -> sqlite3.Connection:
    """Establishes a connection to the SQLite database."""
    try:
        # Using check_same_thread=False for FastAPI's async context,
        # but operations should be carefully managed.
        # For read-only purposes in this service, it's generally safe.
        conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = _dict_factory
        return conn
    except sqlite3.OperationalError as e:
        logger.error(f"FATAL: Database not found at {DATABASE_PATH}. "
                     f"Ensure the database is built before running the app. Details: {e}")
        raise RuntimeError("Database not found") from e

async def find_card_printings_by_name(card_name: str, db_conn: sqlite3.Connection) -> List[CardData]:
    """
    Finds all printings of a specific card by its name, using the local database first.
    If not found in the database, it falls back to the Scryfall API.

    Args:
        card_name: The exact name of the card to look up.
        db_conn: An active SQLite database connection.

    Returns:
        A list of dictionaries, where each dictionary represents a unique printing
        of the card. Returns an empty list if the card is not found anywhere.
    """
    # --- Step 1: Find the canonical name for the given card name ---
    # This handles cases where the input is an alternate name (e.g., "Totec's Spear")
    # and finds its real name ("Shadowspear").
    cursor = db_conn.cursor()
    cursor.execute("SELECT real_name FROM cards WHERE name = ? LIMIT 1", (card_name,))
    result = cursor.fetchone()
    
    canonical_name = card_name # Default to the given name
    if result and result['real_name']:
        canonical_name = result['real_name']
        logger.info(f"Resolved '{card_name}' to its canonical name: '{canonical_name}'.")
    else:
        logger.info(f"Could not resolve a canonical name for '{card_name}'; proceeding with the given name.")

    # --- Step 2: Query the local database for all printings using the canonical name ---
    cursor.execute(
        "SELECT * FROM cards WHERE real_name = ? ORDER BY price_usd ASC",
        (canonical_name,)
    )
    results = cursor.fetchall()
    
    if results:
        logger.info(f"Found {len(results)} printings for '{canonical_name}' in local DB.")
        return results

    # --- Step 3: Fallback to Scryfall API if not found ---
    logger.warning(f"Card '{canonical_name}' not in local DB. Falling back to Scryfall API.")
    await asyncio.sleep(SCRYFALL_REQUEST_DELAY)  # Respect rate limits

    try:
        # Use the /cards/search endpoint with `unique=prints` to get all versions
        # The `!"card name"` syntax is for exact name matching in Scryfall.
        response = await scryfall_client.get(
            "/cards/search",
            params={"q": f'!\"{canonical_name}\" unique:prints', "order": "usd"}
        )
        response.raise_for_status()
        
        data = response.json()
        scryfall_cards = data.get("data", [])
        
        if not scryfall_cards:
            logger.error(f"Card '{card_name}' not found on Scryfall either.")
            return []

        # --- Step 3: Normalize Scryfall data to match our DB schema ---
        normalized_cards: List[CardData] = []
        for card in scryfall_cards:
            # We only care about cards with a USD price for sorting
            prices = card.get("prices", {})
            if "image_uris" in card and "normal" in card["image_uris"]:
                normalized_cards.append({
                    "id": card.get("id"),
                    "name": card.get("name"),
                    "set_code": card.get("set"),
                    "collector_number": card.get("collector_number"),
                    "image_uri_normal": card["image_uris"]["normal"],
                    "scryfall_uri": card.get("scryfall_uri"),
                    "price_usd": float(prices.get("usd")) if prices.get("usd") else None,
                    "price_foil": float(prices.get("usd_foil")) if prices.get("usd_foil") else None,
                })
        
        logger.info(f"Found {len(normalized_cards)} printings for '{card_name}' via Scryfall API.")
        return normalized_cards

    except httpx.HTTPStatusError as e:
        # 404 means not found, which is not an error in our case
        if e.response.status_code == 404:
            logger.error(f"Card '{card_name}' not found on Scryfall (404).")
            return []
        logger.error(f"Scryfall API error for '{card_name}': {e}")
        return [] # Return empty list on other HTTP errors
    except (httpx.RequestError, KeyError) as e:
        logger.error(f"An error occurred while querying Scryfall for '{card_name}': {e}")
        return []
