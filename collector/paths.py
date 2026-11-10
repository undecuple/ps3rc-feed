"""Repository paths, defined once for every module that needs them."""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_ROOT = os.path.join(REPO_ROOT, "tests", "fixtures")
DATA_ROOT = os.path.join(REPO_ROOT, "data")
EXAMPLE_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "sources.example.json")
