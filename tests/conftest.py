"""conftest.py — pytest configuration"""
import sys
import os

# Add project root to path so `src.*` imports work from tests/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
