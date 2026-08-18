from takealot_ops.product_search import (
    matches_product_search,
    normalize_product_search_text,
    product_name_matches,
)


def test_normalizes_product_name_punctuation_and_accents() -> None:
    assert normalize_product_search_text("  Café—Flood_Light  ") == "cafe flood light"


def test_matches_partial_unordered_and_bounded_typo_product_terms() -> None:
    title = "Corduroy Lazy Sofa Chair - Foldable Home Seat"
    assert product_name_matches(title, "chair cord")
    assert product_name_matches(title, "sof ch")
    assert product_name_matches("Wireless Bluetooth Speaker", "speaker wireless")
    assert product_name_matches("Wireless Bluetooth Speaker", "speaker wirless")
    assert product_name_matches("Outdoor Floodlight", "flood light")
    assert not product_name_matches("Wireless Bluetooth Speaker", "rekaeps sseleriw")
    assert not product_name_matches("Portable Charger", "portable crhgra")
    assert not product_name_matches(title, "garden table")


def test_identifiers_keep_substring_only_matching() -> None:
    values = {
        "query": "COMPANY-BLUE",
        "product_names": ["Wireless Bluetooth Speaker"],
        "other_values": ["PLID12345678", "COMPANY-BLUE-01"],
    }
    assert matches_product_search(**values)
    assert not matches_product_search(**{**values, "query": "COMPANI-BLUE"})
    assert not matches_product_search(**{**values, "query": "12345679"})
