
import logging
import os
import time
from typing import List, Optional
from datetime import datetime

import uvicorn
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.parser import parse_decklist, ParsedCard
from app.services.card_service import (
    find_card_printings_by_name,
    get_db_connection,
    CardData,
)

# --- App Configuration ---
app = FastAPI(
    title="Bling My Deck",
    description="Find alternate art/frame versions of cards in your Magic: The Gathering deck.",
)
templates = Jinja2Templates(directory="app/templates")
logging.basicConfig(level=logging.INFO)

# --- Database Connection ---
# This is a global connection for the app instance.
# For a production app with higher concurrency, a connection pool would be better.
try:
    db_connection = get_db_connection()
except RuntimeError as e:
    logging.error(f"Application startup failed: {e}")
    # You might want to exit here if the DB is essential for all routes
    db_connection = None


@app.on_event("shutdown")
def shutdown_event():
    if db_connection:
        db_connection.close()
    logging.info("Database connection closed.")


def _price_value(card: CardData) -> float:
    """Return the primary price used for sorting (foil preferred over non‑foil)."""
    foil_price = card.get("price_foil")
    usd_price = card.get("price_usd")
    if foil_price is not None:
        return float(foil_price)
    if usd_price is not None:
        return float(usd_price)
    # Very low sentinel so price-less cards go to the end when sorting desc
    return -1.0


def _release_date_value(card: CardData) -> datetime:
    """Return a datetime for the card's release date, or a far past date if missing."""
    date_str = card.get("released_at")
    if isinstance(date_str, str):
        try:
            return datetime.fromisoformat(date_str)
        except ValueError:
            pass
    # Use a stable minimal date for missing/invalid values
    return datetime.min


def sort_printings(printings: List[CardData], sort_order: str, only_paper: bool) -> List[CardData]:
    """
    Apply filtering (paper-only) and sorting to a list of card printings.

    sort_order options:
      - 'price_down' (default): most expensive → cheapest
      - 'price_up': cheapest → most expensive
      - 'release_down': newest → oldest
      - 'release_up': oldest → newest
    """
    # Filter out non‑paper printings if requested. DB rows use 0/1, API fallback uses the same.
    if only_paper:
        printings = [p for p in printings if p.get("is_paper")]

    if sort_order == "price_up":
        return sorted(printings, key=_price_value)
    if sort_order == "release_down":
        return sorted(printings, key=_release_date_value, reverse=True)
    if sort_order == "release_up":
        return sorted(printings, key=_release_date_value)

    # Default: price_down (more expensive first)
    return sorted(printings, key=_price_value, reverse=True)


# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """
    Serves the main page with the decklist input form.
    """
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/analyze", response_class=HTMLResponse)
async def analyze_decklist(
    request: Request,
    decklist: str = Form(...),
    sort_order: str = Form("price_down"),
    only_paper: Optional[str] = Form(None),
):
    """
    Processes a submitted decklist, finds alternate card printings,
    and returns an HTML fragment with the results, intended for HTMX swapping.
    """
    start_time = time.perf_counter()
    logging.info(f"Received decklist for analysis: '{decklist}'")
    if not db_connection:
        raise HTTPException(status_code=503, detail="Database connection is not available.")

    # Step 1: Parse the user's decklist
    original_cards: List[ParsedCard] = parse_decklist(decklist)
    if not original_cards:
        end_time = time.perf_counter()
        duration = (end_time - start_time) * 1000 # in ms
        logging.info(f"Analysis for decklist completed in {duration:.2f}ms. No cards parsed.")
        return HTMLResponse(
            content='<div class="error-message">Could not parse any cards from the decklist. Please check the format.</div>',
            status_code=400
        )

    # Normalise form flags
    paper_only_flag = only_paper is not None

    # Step 2: For each card, find all its printings
    results_data = []
    total_cards_requested = 0
    for quantity, name, set_code, coll_num in original_cards:
        total_cards_requested += quantity
        all_printings = await find_card_printings_by_name(name, db_connection)
        
        if not all_printings:
            # Add a placeholder for cards that couldn't be found
            results_data.append({
                "original_card_info": f"{quantity}x {name}",
                "original_card_id": None,
                "printings": [],
                "error": f"Could not find any printings for '{name}'. It might be a new or unofficial card."
            })
            continue

        # Step 3: Filter/sort the printings according to user settings
        all_printings = sort_printings(all_printings, sort_order, paper_only_flag)
        
        # Step 4: Identify the user's specific printing (if provided)
        original_card_id = None
        if set_code and coll_num:
            for p in all_printings:
                # Case-insensitive comparison for set code
                if p.get("set_code", "").lower() == set_code.lower() and p.get("collector_number") == coll_num:
                    original_card_id = p.get("id")
                    break
        
        # If the specific version wasn't found, fall back to the cheapest as the reference
        if not original_card_id:
            original_card_id = all_printings[0].get("id") if all_printings else None

        results_data.append({
            "original_card_info": f"{quantity}x {name}",
            "original_card_id": original_card_id,
            "printings": all_printings,
            "error": None
        })

    # Step 5: Render the results to an HTML partial
    end_time = time.perf_counter()
    duration = (end_time - start_time) * 1000 # in ms
    time_per_card = duration / total_cards_requested if total_cards_requested else 0
    logging.info(
        f"Analysis for decklist completed in {duration:.2f}ms. "
        f"Total cards requested: {total_cards_requested}. Time per card: {time_per_card:.2f}ms."
    )

    # If this is an HTMX request, return just the partial fragment to be swapped
    # into the existing page. Otherwise, render a full page so direct navigation
    # to /analyze still looks nicely formatted.
    is_htmx = request.headers.get("HX-Request") == "true"
    template_name = "_results.html" if is_htmx else "results_full.html"

    return templates.TemplateResponse(
        template_name,
        {"request": request, "results": results_data}
    )


if __name__ == "__main__":
    # This is for local development.
    # The Docker container will use a production-grade server like Gunicorn + Uvicorn workers.
    # Note: Uvicorn's 'port' argument is overridden by the $PORT env var on Cloud Run.
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
