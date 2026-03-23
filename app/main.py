
import logging
import os
import time
import uuid
from typing import List, Optional
from datetime import datetime

import uvicorn
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
import posthog

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


@app.get("/favicon.png", include_in_schema=False)
def favicon_png():
    # Serve directly so we don't have to mount a static directory.
    return FileResponse("static/favicon.png", media_type="image/png")


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    # Some browsers still request /favicon.ico; serve the same PNG.
    return FileResponse("static/favicon.png", media_type="image/png")

# --- Database Connection ---
# This is a global connection for the app instance.
# For a production app with higher concurrency, a connection pool would be better.
try:
    db_connection = get_db_connection()
except RuntimeError as e:
    logging.error(f"Application startup failed: {e}")
    # You might want to exit here if the DB is essential for all routes
    db_connection = None

# --- PostHog Analytics ---
POSTHOG_API_KEY = os.environ.get("POSTHOG_API_KEY", "phc_H7IVdBRysGFlumQD3AJWZ7ertz7hueDDCfeoW0tPAhp")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")

try:
    posthog_client = posthog.Posthog(
        project_api_key=POSTHOG_API_KEY,
        host=POSTHOG_HOST,
        flush_interval=10,  # Flush every 10s
    )
except Exception as e:
    logging.error(f"Failed to initialize PostHog client: {e}")
    posthog_client = None

# Limit how many candidate-printing events we emit per card input.
# These controls reduce PostHog egress for large or "printing-heavy" cards.
POSTHOG_CANDIDATES_MAX_PER_LOOKUP = int(os.environ.get("POSTHOG_CANDIDATES_MAX_PER_LOOKUP", "10"))
POSTHOG_CANDIDATES_MAX_TOTAL_PER_REQUEST = int(
    os.environ.get("POSTHOG_CANDIDATES_MAX_TOTAL_PER_REQUEST", "200")
)

POSTHOG_EVENT_DECK_CARD_LOOKUP = "deck_card_lookup"
POSTHOG_EVENT_DECK_CARD_CANDIDATE = "deck_card_candidate"
POSTHOG_EVENT_DECK_CARD_LOOKUP_MISS = "deck_card_lookup_miss"
POSTHOG_EVENT_DECK_CARD_LOOKUP_FILTERED_EMPTY = "deck_card_lookup_filtered_empty"


def get_distinct_id(request: Request) -> str:
    """Get PostHog distinct_id from cookies or generate one from session."""
    import urllib.parse
    import json
    
    # Try various PostHog cookie formats
    # PostHog typically sets cookies like: ph_<project_id>_posthog or phc_<project_id>_posthog
    for cookie_name in request.cookies.keys():
        if 'posthog' in cookie_name.lower() and ('ph_' in cookie_name or 'phc_' in cookie_name):
            cookie_value = request.cookies.get(cookie_name)
            if cookie_value:
                try:
                    # Try to decode URL-encoded JSON if present
                    decoded = urllib.parse.unquote(cookie_value)
                    # Check if it's JSON
                    if decoded.startswith('{'):
                        data = json.loads(decoded)
                        if 'distinct_id' in data:
                            return data['distinct_id']
                    # If not JSON, return the value directly
                    return cookie_value
                except (json.JSONDecodeError, ValueError):
                    # If parsing fails, return the raw value
                    return cookie_value
    
    # Try to get from PostHog session ID header (if sent by client)
    ph_session_id = request.headers.get("X-PostHog-Session-ID")
    if ph_session_id:
        return ph_session_id
    
    # Fallback: use session ID or generate from IP + user agent
    session_id = request.cookies.get("session_id")
    if session_id:
        return session_id
    
    # Last resort: use a hash of IP + user agent for anonymous tracking
    # PostHog will merge this with client-side distinct_id if user is identified later
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")
    import hashlib
    return hashlib.md5(f"{client_ip}{user_agent}".encode()).hexdigest()


@app.on_event("shutdown")
def shutdown_event():
    if db_connection:
        db_connection.close()
    # Flush PostHog events before shutdown (critical for serverless)
    if posthog_client:
        try:
            posthog_client.shutdown()
        except Exception as e:
            logging.error(f"Error shutting down PostHog: {e}")
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


def expand_printings_to_variants(printings: List[CardData], only_paper: bool) -> List[dict]:
    """
    Expand each DB printing into 1 or 2 UI variants:
    - nonfoil variant when `price_usd` exists
    - foil variant when `price_foil` exists
    If neither price exists, we still emit a single 'nonfoil' variant with `variant_price=None`.
    """
    variants: List[dict] = []
    for p in printings:
        if only_paper and not p.get("is_paper"):
            continue

        price_usd = p.get("price_usd")
        price_foil = p.get("price_foil")

        nonfoil_included = price_usd is not None
        foil_included = price_foil is not None

        if not nonfoil_included and not foil_included:
            variants.append(
                {
                    **p,
                    "variant_type": "nonfoil",
                    "variant_price": None,
                }
            )
            continue

        if nonfoil_included:
            variants.append(
                {
                    **p,
                    "variant_type": "nonfoil",
                    "variant_price": price_usd,
                }
            )
        if foil_included:
            variants.append(
                {
                    **p,
                    "variant_type": "foil",
                    "variant_price": price_foil,
                }
            )

    return variants


def sort_variants(variants: List[dict], sort_order: str) -> List[dict]:
    """Sort by variant price (price_*), otherwise by release date."""
    if sort_order in ("price_down", "price_up"):
        # Put None prices last regardless of direction.
        def key(v: dict) -> float:
            vp = v.get("variant_price")
            if vp is None:
                return float("-inf") if sort_order == "price_down" else float("inf")
            return float(vp)

        reverse = sort_order == "price_down"
        return sorted(variants, key=key, reverse=reverse)

    # Release sorting: variant_price doesn't matter (both variants share the same released_at).
    # Reuse existing helper; it expects a CardData-like dict with `released_at`.
    if sort_order == "release_down":
        return sorted(variants, key=_release_date_value, reverse=True)
    if sort_order == "release_up":
        return sorted(variants, key=_release_date_value)
    return sorted(variants, key=_release_date_value, reverse=True)


# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """
    Serves the main page with the decklist input form.
    """
    # Starlette 0.50+: first arg must be Request (not template name).
    return templates.TemplateResponse(request, "index.html")


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

    # Get distinct_id for PostHog tracking (with error handling)
    try:
        distinct_id = get_distinct_id(request)
    except Exception as e:
        logging.warning(f"Error getting distinct_id for PostHog: {e}")
        distinct_id = "anonymous"

    # Step 2: For each card, find all its printings
    results_data = []
    total_cards_requested = 0
    deck_request_id = uuid.uuid4().hex
    candidates_logged = 0
    for quantity, name, set_code, coll_num, input_is_foil in original_cards:
        total_cards_requested += quantity
        input_set_code = set_code.upper() if set_code else None
        input_collector_number = coll_num

        all_printings = await find_card_printings_by_name(name, db_connection)
        resolved_real_name = None
        if all_printings:
            resolved_real_name = all_printings[0].get("real_name") or all_printings[0].get("name") or name
        else:
            resolved_real_name = name

        if not all_printings:
            # Add a placeholder for cards that couldn't be found
            results_data.append({
                "original_card_info": f"{quantity}x {name}",
                "original_card_id": None,
                "printings": [],
                "error": f"Could not find any printings for '{name}'. It might be a new or unofficial card."
            })

            if posthog_client:
                try:
                    posthog_client.capture(
                        distinct_id=distinct_id,
                        event=POSTHOG_EVENT_DECK_CARD_LOOKUP_MISS,
                        properties={
                            "deck_request_id": deck_request_id,
                            "input_card_name": name,
                            "input_set_code": input_set_code,
                            "input_collector_number": input_collector_number,
                            "input_is_fully_specified": bool(input_set_code and input_collector_number),
                            "resolved_real_name": resolved_real_name,
                            "quantity": quantity,
                            "sort_order": sort_order,
                            "paper_only": paper_only_flag,
                            "printings_found": 0,
                        },
                    )
                except Exception as e:
                    logging.error(f"Error sending PostHog miss event: {e}")

            continue

        # Step 3: Expand to UI variants (foil/non-foil separate) and sort.
        variants = expand_printings_to_variants(all_printings, paper_only_flag)

        if not variants:
            results_data.append(
                {
                    "original_card_info": f"{quantity}x {name}",
                    "original_card_id": None,
                    "printings": [],
                    "error": "No printings matched the requested filters.",
                }
            )

            if posthog_client:
                try:
                    posthog_client.capture(
                        distinct_id=distinct_id,
                        event=POSTHOG_EVENT_DECK_CARD_LOOKUP_FILTERED_EMPTY,
                        properties={
                            "deck_request_id": deck_request_id,
                            "input_card_name": name,
                            "input_set_code": input_set_code,
                            "input_collector_number": input_collector_number,
                            "input_is_fully_specified": bool(input_set_code and input_collector_number),
                            "resolved_real_name": resolved_real_name,
                            "quantity": quantity,
                            "sort_order": sort_order,
                            "paper_only": paper_only_flag,
                            "printings_found": 0,
                        },
                    )
                except Exception as e:
                    logging.error(f"Error sending PostHog filtered-empty event: {e}")

            continue

        # Step 4: Sort variants and choose the reference variant to highlight.
        sorted_variants = sort_variants(variants, sort_order)

        matched_input_printing_id = None
        if input_set_code and input_collector_number:
            # Find a printing (card id) that matches set + collector in the filtered variants.
            for v in variants:
                if (
                    v.get("set_code", "").lower() == input_set_code.lower()
                    and v.get("collector_number") == input_collector_number
                ):
                    matched_input_printing_id = v.get("id")
                    break

        if matched_input_printing_id:
            candidates = [v for v in sorted_variants if v.get("id") == matched_input_printing_id]
            if candidates:
                if input_is_foil is not None:
                    wanted_variant_type = "foil" if input_is_foil else "nonfoil"
                    ref = next(
                        (vv for vv in candidates if vv.get("variant_type") == wanted_variant_type),
                        None,
                    )
                    reference_variant = ref or candidates[0]
                else:
                    reference_variant = candidates[0]
            else:
                reference_variant = sorted_variants[0]
        else:
            reference_variant = sorted_variants[0]

        reference_card_id = reference_variant.get("id")
        reference_index = sorted_variants.index(reference_variant)
        reference_price_usd = reference_variant.get("price_usd")
        reference_price_foil = reference_variant.get("price_foil")

        for v in sorted_variants:
            v["is_reference"] = v is reference_variant

        has_foil = any(v.get("variant_type") == "foil" for v in sorted_variants)

        if posthog_client:
            try:
                posthog_client.capture(
                    distinct_id=distinct_id,
                    event=POSTHOG_EVENT_DECK_CARD_LOOKUP,
                    properties={
                        "deck_request_id": deck_request_id,
                        "input_card_name": name,
                        "input_set_code": input_set_code,
                        "input_collector_number": input_collector_number,
                        "input_is_fully_specified": bool(input_set_code and input_collector_number),
                        "resolved_real_name": resolved_real_name,
                        "quantity": quantity,
                        "sort_order": sort_order,
                        "paper_only": paper_only_flag,
                        "printings_found": len(all_printings),
                        "has_foil": has_foil,
                        "matched_input_printing_id": matched_input_printing_id,
                        "reference_card_id": reference_card_id,
                        "reference_index": reference_index,
                        "reference_set_code": reference_variant.get("set_code") if reference_variant else None,
                        "reference_collector_number": reference_variant.get("collector_number") if reference_variant else None,
                        "reference_price_usd": reference_price_usd,
                        "reference_price_foil": reference_price_foil,
                        "input_is_foil": input_is_foil,
                    },
                )

                # Emit bounded per-printing candidate events for SQL-friendly analysis.
                remaining = POSTHOG_CANDIDATES_MAX_TOTAL_PER_REQUEST - candidates_logged
                if remaining > 0:
                    max_for_this_lookup = min(POSTHOG_CANDIDATES_MAX_PER_LOOKUP, remaining)
                    for candidate_index, p in enumerate(all_printings[:max_for_this_lookup]):
                        posthog_client.capture(
                            distinct_id=distinct_id,
                            event=POSTHOG_EVENT_DECK_CARD_CANDIDATE,
                            properties={
                                "deck_request_id": deck_request_id,
                                "input_card_name": name,
                                "input_set_code": input_set_code,
                                "input_collector_number": input_collector_number,
                                "resolved_real_name": resolved_real_name,
                                "quantity": quantity,
                                "sort_order": sort_order,
                                "paper_only": paper_only_flag,
                                "candidate_index": candidate_index,
                                "candidate_card_id": p.get("id"),
                                "candidate_set_code": p.get("set_code"),
                                "candidate_collector_number": p.get("collector_number"),
                                "candidate_is_reference": p.get("id") == reference_card_id,
                                "candidate_is_input_match": p.get("id") == matched_input_printing_id,
                                "candidate_price_usd": p.get("price_usd"),
                                "candidate_price_foil": p.get("price_foil"),
                                "candidate_foil_type": p.get("foil_type"),
                                "candidate_available_nonfoil": p.get("price_usd") is not None,
                                "candidate_available_foil": p.get("price_foil") is not None,
                                "candidate_is_paper": p.get("is_paper"),
                            },
                        )
                    candidates_logged += max_for_this_lookup
            except Exception as e:
                logging.error(f"Error sending PostHog lookup/candidate events: {e}")

        results_data.append({
            "original_card_info": f"{quantity}x {name}",
            "original_card_id": reference_card_id,
            "printings": sorted_variants,
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

    # Always return the partial fragment for HTMX swapping
    return templates.TemplateResponse(
        request,
        "_results.html",
        {"results": results_data},
    )


if __name__ == "__main__":
    # This is for local development.
    # The Docker container will use a production-grade server like Gunicorn + Uvicorn workers.
    # Note: Uvicorn's 'port' argument is overridden by the $PORT env var on Cloud Run.
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
