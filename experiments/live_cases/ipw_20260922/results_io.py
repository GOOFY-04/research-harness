"""results_io.py

Writes runtime paired sample data to JSON files for harness validation.
Uses only the Python standard library.
"""

import json
import os


def write_pairs_json(path: str, pairs: list[dict]) -> None:
    """Write a list of paired sample dictionaries to a JSON file.

    Creates the parent directory if it does not already exist.
    """
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pairs, f, indent=2, ensure_ascii=False)


def write_experiment_contract(path: str, contract: dict) -> None:
    """Write the experiment contract dictionary to a JSON file.

    Creates the parent directory if it does not already exist.
    """
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(contract, f, indent=2, ensure_ascii=False)