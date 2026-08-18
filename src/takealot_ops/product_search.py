"""Shared product-name fuzzy matching for server-side paginated searches."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from typing import Any


def normalize_product_search_text(value: Any) -> str:
    """Normalize case, accents, punctuation, and whitespace for product names."""
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.category(character).startswith("M")
    ).casefold()
    normalized = "".join(
        character if character.isalnum() else " " for character in without_marks
    )
    return " ".join(normalized.split())


def product_name_matches(value: Any, query: Any) -> bool:
    """Match unordered partial words with bounded typos, never by anagrams."""
    normalized_name = normalize_product_search_text(value)
    normalized_query = normalize_product_search_text(query)
    if not normalized_query:
        return True
    if not normalized_name:
        return False
    if normalized_query in normalized_name:
        return True
    if normalized_query.replace(" ", "") in normalized_name.replace(" ", ""):
        return True

    name_tokens = normalized_name.split()
    return all(
        any(_fuzzy_token_matches(name_token, query_token) for name_token in name_tokens)
        for query_token in normalized_query.split()
    )


def matches_product_search(
    query: Any,
    *,
    product_names: Sequence[Any] = (),
    other_values: Sequence[Any] = (),
) -> bool:
    """Fuzz product names while retaining substring semantics for identifiers."""
    exact_query = str(query or "").strip().casefold()
    if not exact_query:
        return True
    if any(exact_query in str(value or "").casefold() for value in other_values):
        return True
    return any(product_name_matches(value, query) for value in product_names)


def _fuzzy_token_matches(name_token: str, query_token: str) -> bool:
    if query_token in name_token:
        return True
    if not any(character.isalpha() for character in query_token):
        return False
    if not any(character.isalpha() for character in name_token):
        return False
    # Preserve character order: this is bounded typo tolerance, not an anagram match.
    maximum_distance = 2 if len(query_token) >= 9 else 1 if len(query_token) >= 5 else 0
    if not maximum_distance:
        return False
    if abs(len(name_token) - len(query_token)) > maximum_distance:
        return False
    return _edit_distance_within(name_token, query_token, maximum_distance)


def _edit_distance_within(left: str, right: str, limit: int) -> bool:
    if abs(len(left) - len(right)) > limit:
        return False
    matrix = [[0] * (len(right) + 1) for _ in range(len(left) + 1)]
    for index in range(len(left) + 1):
        matrix[index][0] = index
    for index in range(len(right) + 1):
        matrix[0][index] = index

    for left_index in range(1, len(left) + 1):
        for right_index in range(1, len(right) + 1):
            substitution_cost = left[left_index - 1] != right[right_index - 1]
            matrix[left_index][right_index] = min(
                matrix[left_index - 1][right_index] + 1,
                matrix[left_index][right_index - 1] + 1,
                matrix[left_index - 1][right_index - 1] + substitution_cost,
            )
            if (
                left_index > 1
                and right_index > 1
                and left[left_index - 1] == right[right_index - 2]
                and left[left_index - 2] == right[right_index - 1]
            ):
                # One adjacent swap is a typing error, not arbitrary letter reordering.
                matrix[left_index][right_index] = min(
                    matrix[left_index][right_index],
                    matrix[left_index - 2][right_index - 2] + 1,
                )
    return matrix[-1][-1] <= limit
