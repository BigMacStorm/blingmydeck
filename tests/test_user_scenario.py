
import sys
import os
import pytest
from fastapi.testclient import TestClient
from bs4 import BeautifulSoup

from app.main import app

client = TestClient(app)

def test_user_scenario():
    """
    Tests a full user scenario, from submitting a decklist to verifying the results.
    """
    decklist = """
1 Amalia Benavides Aguirre (LCI) 299
1 Sol Ring (2ED) 270
1 Sorin of House Markov / Sorin, Ravenous Neonate (MH3) 470 *F*
4 Swamp (NEO) 298 *F*
1 Totec's Spear (SLD) 1505 *F*

SIDEBOARD:
1 Food (SLD) 1938 *F*
"""

    response = client.post("/analyze", data={"decklist": decklist})
    assert response.status_code == 200

    soup = BeautifulSoup(response.content, "html.parser")

    # --- Helper to find a card's result section ---
    def find_card_section(name_in_header):
        all_headers = soup.find_all("h2", class_="grid-header")
        for header in all_headers:
            if name_in_header in header.text:
                return header.parent
        return None

    # --- 1. Verify Amalia Benavides Aguirre ---
    amalia_section = find_card_section("Amalia Benavides Aguirre")
    assert amalia_section is not None, "Section for Amalia not found."
    printings = amalia_section.find_all("div", class_="card-container")
    assert len(printings) > 1, "Expected multiple printings for Amalia."
    # Check for a specific alternate art version
    assert any(
        (img := p.find("img")) is not None and 
        (alt := img.get("alt")) is not None and 
        "lci" in str(alt).lower() 
        for p in printings
    ), "Expected LCI version of Amalia."

    # --- 2. Verify Sol Ring ---
    sol_ring_section = find_card_section("Sol Ring")
    assert sol_ring_section is not None, "Section for Sol Ring not found."
    printings = sol_ring_section.find_all("div", class_="card-container")
    assert len(printings) > 1, "Expected multiple printings for Sol Ring."
    # Check for a specific alternate art version (e.g., from a Commander set)
    assert any(
        (img := p.find("img")) is not None and 
        (alt := img.get("alt")) is not None and 
        "ltc" in str(alt).lower() 
        for p in printings
    ), "Expected LTC version of Sol Ring."
    assert any(
        (img := p.find("img")) is not None and 
        (alt := img.get("alt")) is not None and 
        "c21" in str(alt).lower() 
        for p in printings
    ), "Expected C21 version of Sol Ring."

    # --- 3. Verify Sorin of House Markov ---
    sorin_section = find_card_section("Sorin of House Markov // Sorin, Ravenous Neonate")
    assert sorin_section is not None, "Section for Sorin not found."
    printings = sorin_section.find_all("div", class_="card-container")
    assert len(printings) > 1, "Expected multiple printings for Sorin."
    assert any(
        (img := p.find("img")) is not None and 
        (alt := img.get("alt")) is not None and 
        "mh3" in str(alt).lower() 
        for p in printings
    ), "Expected MH3 version of Sorin."

    # --- 4. Verify Swamp ---
    swamp_section = find_card_section("Swamp")
    assert swamp_section is not None, "Section for Swamp not found."
    printings = swamp_section.find_all("div", class_="card-container")
    assert len(printings) > 1, "Expected multiple printings for Swamp."
    # Check for a basic land from a different set
    assert any(
        (img := p.find("img")) is not None and 
        (alt := img.get("alt")) is not None and 
        "neo" in str(alt).lower() 
        for p in printings
    ), "Expected NEO version of Swamp."
    assert any(
        (img := p.find("img")) is not None and 
        (alt := img.get("alt")) is not None and 
        "unf" in str(alt).lower() 
        for p in printings
    ), "Expected UNF version of Swamp."

    # --- 5. Verify Shadowspear (aliased as Totec's Spear) ---
    shadowspear_section = find_card_section("Totec's Spear")
    assert shadowspear_section is not None, "Section for Totec's Spear not found."
    printings = shadowspear_section.find_all("div", class_="card-container")
    assert len(printings) > 1, "Expected multiple printings for Shadowspear."
    # Check that the real card name is present in the printings
    assert any(
        (img := p.find("img")) is not None and 
        (alt := img.get("alt")) is not None and 
        "shadowspear" in str(alt).lower() 
        for p in printings
    ), "Expected to find Shadowspear printings."
    assert any(
        (img := p.find("img")) is not None and 
        (alt := img.get("alt")) is not None and 
        "sld" in str(alt).lower() 
        for p in printings
    ), "Expected SLD version of Shadowspear."

    # --- 6. Verify Food (from sideboard) ---
    food_section = find_card_section("Food")
    assert food_section is not None, "Section for Food not found."
    printings = food_section.find_all("div", class_="card-container")
    assert len(printings) > 1, "Expected multiple printings for Food."
    assert any(
        (img := p.find("img")) is not None and 
        (alt := img.get("alt")) is not None and 
        "sld" in str(alt).lower() 
        for p in printings
    ), "Expected SLD version of Food."
