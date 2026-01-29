"""Restaurant recommendation agent.

Run: uv run python -m bishkek_food_finder.agent "где вкусный плов"
     uv run python -m bishkek_food_finder.agent -i  # interactive mode
"""

from .core import run, main, MODEL, MAX_ITERATIONS, MAX_HISTORY_MESSAGES
from .tools import TOOLS, MAX_RESTAURANTS, MAX_REVIEWS, N_REVIEWS

__all__ = [
    "run",
    "main",
    "MODEL",
    "MAX_ITERATIONS",
    "MAX_HISTORY_MESSAGES",
    "TOOLS",
    "MAX_RESTAURANTS",
    "MAX_REVIEWS",
    "N_REVIEWS",
]
