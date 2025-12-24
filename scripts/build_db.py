
import json
import logging
import os
import sqlite3
import time
import unicodedata
from pathlib import Path

import requests

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
# Scryfall API URL for all bulk data objects
BULK_DATA_API_URL = "https://api.scryfall.com/bulk-data"
# The type of bulk data we are interested in
BULK_DATA_TYPE = "default_cards"
# Path to the directory where data will be stored, relative to the project root
DATA_DIR = Path("app/data")
# Database file name
DB_FILENAME = "cards.db"
# Full path to the database file
DATABASE_PATH = DATA_DIR / DB_FILENAME
# Path for the temporary downloaded JSON file
JSON_TMP_PATH = DATA_DIR / "default_cards.json"

# --- Database Schema ---
TABLE_NAME = "cards"
CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    real_name TEXT NOT NULL,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    image_uri_normal TEXT,
    scryfall_uri TEXT NOT NULL,
    price_usd REAL,
    price_foil REAL
);
"""
CREATE_INDEX_SQL = f"CREATE INDEX IF NOT EXISTS idx_card_name_real_name ON {TABLE_NAME}(name COLLATE NOCASE, real_name COLLATE NOCASE);"

def get_bulk_data_url() -> str:
    """
    Fetches the URL for the 'Default Cards' bulk data file from Scryfall.
    """
    logging.info("Fetching bulk data metadata from Scryfall...")
    try:
        response = requests.get(BULK_DATA_API_URL, timeout=60)
        response.raise_for_status()
        all_bulk_data = response.json()["data"]
        # Find the 'default_cards' data object
        for data_object in all_bulk_data:
            if data_object.get("type") == BULK_DATA_TYPE:
                download_url = data_object["download_uri"]
                logging.info(f"Found '{BULK_DATA_TYPE}' download URL.")
                return download_url
        raise RuntimeError(f"Could not find bulk data of type '{BULK_DATA_TYPE}'.")
    except (requests.RequestException, KeyError, RuntimeError) as e:
        logging.error(f"Failed to get bulk data URL: {e}")
        raise

def download_bulk_data(url: str):
    """
    Streams the download of the bulk data JSON file to avoid high memory usage.
    """
    logging.info(f"Downloading bulk data from {url}...")
    try:
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(JSON_TMP_PATH, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        logging.info(f"Successfully downloaded and saved to {JSON_TMP_PATH}")
    except requests.RequestException as e:
        logging.error(f"Failed to download bulk data: {e}")
        raise

def create_database_and_tables(conn: sqlite3.Connection):
    """
    Creates the database tables and indexes.
    """
    logging.info("Creating database tables and indexes...")
    try:
        cursor = conn.cursor()
        cursor.execute(CREATE_TABLE_SQL)
        cursor.execute(CREATE_INDEX_SQL)
        conn.commit()
        logging.info("Database structure created successfully.")
    except sqlite3.Error as e:
        logging.error(f"Database setup failed: {e}")
        raise

def process_and_insert_data(conn: sqlite3.Connection):
    """
    Reads the JSON data, processes it, and inserts it into the SQLite database.
    """
    logging.info(f"Processing JSON file: {JSON_TMP_PATH}")
    
    # Use a high-memory approach for speed, as this is a build script.
    with open(JSON_TMP_PATH, "r", encoding="utf-8") as f:
        all_cards = json.load(f)

    cursor = conn.cursor()
    
    insert_count = 0
    batch = []
    start_time = time.time()

    for card in all_cards:
        # --- Correctly find the image URI ---
        image_uri = None
        # Case 1: Standard single-faced card
        if "image_uris" in card and "normal" in card["image_uris"]:
            image_uri = card["image_uris"]["normal"]
        # Case 2: Multi-faced card (transform, modal_dfc, etc.)
        elif card.get("card_faces") and "image_uris" in card["card_faces"][0]:
            image_uri = card["card_faces"][0]["image_uris"].get("normal")

        # --- Only proceed if we have the data we need ---
        if image_uri and card.get("name"):
            prices = card.get("prices", {})
            usd_price = prices.get("usd")
            usd_foil_price = prices.get("usd_foil")

            card_name = unicodedata.normalize('NFC', card["name"])

            # --- Handle Alternate Printed Name (e.g., Totec's Spear for Shadowspear) ---
            # If printed_name exists and is different, it's an alternate art/name.
            # The 'name' column will store this alternate name, but 'real_name'
            # will store the canonical Scryfall name for grouping.
            printed_name = card.get("printed_name")
            if not printed_name:
                printed_name = card.get("flavor_name")

            if printed_name:
                printed_name = unicodedata.normalize('NFC', printed_name)
                if printed_name.lower() != card_name.lower():
                    # This is a true alternate name.
                    batch.append((
                        card["id"],
                        printed_name,      # The name as printed on the card
                        card_name,         # The canonical name
                        card["set"],
                        card["collector_number"],
                        image_uri,
                        card["scryfall_uri"],
                        float(usd_price) if usd_price else None,
                        float(usd_foil_price) if usd_foil_price else None,
                    ))
                else:
                    # printed_name is the same as the main name, not a real alternate
                    batch.append((
                        card["id"],
                        card_name,
                        card_name, # real_name is the same as name
                        card["set"],
                        card["collector_number"],
                        image_uri,
                        card["scryfall_uri"],
                        float(usd_price) if usd_price else None,
                        float(usd_foil_price) if usd_foil_price else None,
                    ))
            else:
                 # Standard card with no alternate printed name
                batch.append((
                    card["id"],
                    card_name,
                    card_name, # real_name is the same as name
                    card["set"],
                    card["collector_number"],
                    image_uri,
                    card["scryfall_uri"],
                    float(usd_price) if usd_price else None,
                    float(usd_foil_price) if usd_foil_price else None,
                ))

        # Insert in batches to improve performance
        if len(batch) >= 1000:
            cursor.executemany(
                f"INSERT OR IGNORE INTO {TABLE_NAME} (id, name, real_name, set_code, collector_number, image_uri_normal, scryfall_uri, price_usd, price_foil) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                batch
            )
            insert_count += len(batch)
            batch = []
    
    # Insert any remaining records
    if batch:
        cursor.executemany(
            f"INSERT OR IGNORE INTO {TABLE_NAME} (id, name, real_name, set_code, collector_number, image_uri_normal, scryfall_uri, price_usd, price_foil) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            batch
        )

    conn.commit()
    end_time = time.time()
    logging.info(f"Inserted {insert_count} card records in {end_time - start_time:.2f} seconds.")

def cleanup():
    """
    Removes the temporary JSON file.
    """
    if os.path.exists(JSON_TMP_PATH):
        logging.info(f"Cleaning up temporary file: {JSON_TMP_PATH}")
        os.remove(JSON_TMP_PATH)
        logging.info("Cleanup complete.")

def main():
    """
    Main function to orchestrate the database build process.
    """
    logging.info("--- Starting Scryfall DB Build Process ---")
    
    # Ensure the data directory exists
    DATA_DIR.mkdir(exist_ok=True)

    # Always rebuild the database to ensure data is fresh and normalized.
    if DATABASE_PATH.exists():
        logging.info("Database already exists. Deleting it to rebuild with latest data.")
        os.remove(DATABASE_PATH)

    conn = None
    try:
        # Step 1: Get the download URL
        download_url = get_bulk_data_url()
        
        # Step 2: Download the bulk data
        download_bulk_data(download_url)
        
        # Step 3: Connect to SQLite and set up
        conn = sqlite3.connect(DATABASE_PATH)
        create_database_and_tables(conn)
        
        # Step 4: Process JSON and insert into DB
        process_and_insert_data(conn)
        
        logging.info("--- Scryfall DB Build Process Completed Successfully ---")

    except Exception as e:
        logging.error(f"An error occurred during the build process: {e}")
        # Exit with a non-zero code to fail the Docker build if something goes wrong
        exit(1)
    finally:
        if conn:
            conn.close()
        # Step 5: Clean up the large JSON file
        cleanup()

if __name__ == "__main__":
    main()
