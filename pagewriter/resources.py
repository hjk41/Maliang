"""Resources travel inside the Python package, including in frozen builds."""
from pathlib import Path


def resource_path(*parts):
    return Path(__file__).resolve().parent.joinpath("resources", *parts)
