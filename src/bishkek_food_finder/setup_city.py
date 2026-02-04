"""Setup a new city end-to-end.

Usage:
    uv run python -m bishkek_food_finder.setup_city almaty           # Full setup
    uv run python -m bishkek_food_finder.setup_city almaty --test    # Test mode (small data, cleanup)
    uv run python -m bishkek_food_finder.setup_city almaty --step reviews  # Run specific step only
"""

import argparse
import os
import shutil
import subprocess
import sqlite3
import sys
from datetime import datetime

from bishkek_food_finder.config import CITIES, get_city_config

TEST_PAGES = 2
TEST_RESTAURANTS = 10

STEP_NAMES = {
    "restaurants": "Scrape Restaurants",
    "reviews": "Scrape Reviews",
    "trust": "Compute Trust Scores",
    "embeddings": "Build Embeddings",
}


def run_cmd(cmd: list[str]) -> bool:
    """Run command with real-time output. Returns success status."""
    result = subprocess.run(cmd)
    return result.returncode == 0


def get_db_stats(db_path: str) -> dict:
    """Get database statistics."""
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    stats = {}
    try:
        stats["restaurants"] = conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]
    except sqlite3.OperationalError:
        stats["restaurants"] = 0
    try:
        stats["reviews"] = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    except sqlite3.OperationalError:
        stats["reviews"] = 0
    try:
        stats["trust_total"] = conn.execute("SELECT COUNT(*) FROM review_trust").fetchone()[0]
        stats["burst_flagged"] = conn.execute(
            "SELECT COUNT(*) FROM review_trust WHERE burst < 1.0"
        ).fetchone()[0]
        stats["trusted"] = conn.execute(
            "SELECT COUNT(*) FROM review_trust WHERE base_trust * burst * recency >= 0.3"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        pass
    conn.close()
    return stats


def get_chroma_count(chroma_path: str) -> int:
    """Get vector count from Chroma collection."""
    if not os.path.exists(chroma_path):
        return 0
    try:
        import chromadb
        client = chromadb.PersistentClient(path=chroma_path)
        collection = client.get_collection("reviews")
        return collection.count()
    except Exception:
        return 0


def print_header(city_name: str, city_code: str, test: bool):
    print()
    print("=" * 60)
    print("  CITY SETUP")
    print("=" * 60)
    print()
    print(f"  City:  {city_name} ({city_code})")
    print(f"  Mode:  {'TEST' if test else 'FULL'}")
    if test:
        print(f"         {TEST_PAGES} pages, {TEST_RESTAURANTS} restaurants, cleanup after")
    print()


def print_step_start(step_num: int, total_steps: int, step_name: str, cmd: list[str]):
    print()
    print("-" * 60)
    print(f"  [{step_num}/{total_steps}] {step_name}")
    print("-" * 60)
    print()
    print(f"  $ {' '.join(cmd)}")
    print()
    print("  Running...")


def print_step_success(stats_lines: list[str]):
    print()
    for line in stats_lines:
        print(f"  {line}")


def print_step_failure(resume_cmd: str):
    print()
    print("  FAILED")
    print()
    print(f"  To resume from this step:")
    print(f"    {resume_cmd}")
    print()


def print_summary(config: dict, test: bool, elapsed: float, stats: dict, chroma_count: int):
    print()
    print("=" * 60)
    print("  COMPLETE")
    print("=" * 60)
    print()
    print(f"  City:   {config['name']}")
    print(f"  Mode:   {'TEST' if test else 'FULL'}")
    print(f"  Time:   {elapsed:.0f}s")
    print()
    print("  Files:")
    if os.path.exists(config['db_path']):
        size = os.path.getsize(config['db_path']) / 1024 / 1024
        print(f"    {config['db_path']} ({size:.1f} MB)")
    if os.path.exists(config['chroma_path']):
        print(f"    {config['chroma_path']}/")
    print()
    print("  Data:")
    print(f"    Restaurants:  {stats.get('restaurants', 0):,}")
    print(f"    Reviews:      {stats.get('reviews', 0):,}")
    print(f"    Trusted:      {stats.get('trusted', 0):,}")
    print(f"    Vectors:      {chroma_count:,}")
    print()


def cleanup(config: dict):
    print("  Cleaning up test data...")
    if os.path.exists(config['db_path']):
        os.remove(config['db_path'])
        print(f"    Deleted: {config['db_path']}")
    if os.path.exists(config['chroma_path']):
        shutil.rmtree(config['chroma_path'])
        print(f"    Deleted: {config['chroma_path']}/")
    print()


def build_resume_cmd(args, step: str) -> str:
    """Build command to resume from a specific step."""
    parts = ["uv run python -m bishkek_food_finder.setup_city", args.city]
    if args.test:
        parts.append("--test")
    parts.extend(["--step", step])
    return " ".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Setup a new city end-to-end")
    parser.add_argument("city", choices=list(CITIES.keys()), help="City to setup")
    parser.add_argument("--test", action="store_true", help="Test mode: small data, cleanup after")
    parser.add_argument(
        "--step",
        choices=["restaurants", "reviews", "trust", "embeddings"],
        help="Run only a specific step"
    )
    parser.add_argument("--skip-scrape", action="store_true", help="Skip scraping, only run indexers")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep test data after run")
    args = parser.parse_args()

    config = get_city_config(args.city, test=args.test)
    print_header(config['name'], args.city, args.test)

    start = datetime.now()
    all_steps = ["restaurants", "reviews", "trust", "embeddings"]

    if args.step:
        steps = [args.step]
    elif args.skip_scrape:
        steps = ["trust", "embeddings"]
    else:
        steps = all_steps

    total_steps = len(steps)
    test_flag = ["--test"] if args.test else []

    for i, step in enumerate(steps, 1):
        # Build command
        if step == "restaurants":
            # Run scraper for both "еда" and "кофе" search terms
            for search_term in ["еда", "кофейня"]:
                cmd = ["uv", "run", "python", "-m", "bishkek_food_finder.scraper.restaurants",
                       "--city", args.city, "--search-term", search_term] + test_flag
                if args.test:
                    cmd.extend(["--pages", str(TEST_PAGES)])

                print_step_start(i, total_steps, f"{STEP_NAMES[step]} ({search_term})", cmd)
                success = run_cmd(cmd)

                if not success:
                    print_step_failure(build_resume_cmd(args, step))
                    sys.exit(1)

            # Show stats after both terms scraped
            stats = get_db_stats(config['db_path'])
            print_step_success([
                f"Restaurants: {stats.get('restaurants', 0):,}",
                f"Saved to: {config['db_path']}",
            ])
            continue  # Skip the common run logic below

        elif step == "reviews":
            cmd = ["uv", "run", "python", "-m", "bishkek_food_finder.scraper.reviews",
                   "--city", args.city] + test_flag
            if args.test:
                cmd.extend(["--limit", str(TEST_RESTAURANTS)])

        elif step == "trust":
            cmd = ["uv", "run", "python", "-m", "bishkek_food_finder.indexer.trust",
                   "--city", args.city] + test_flag

        elif step == "embeddings":
            cmd = ["uv", "run", "python", "-m", "bishkek_food_finder.indexer.embeddings",
                   "--city", args.city] + test_flag

        # Run
        print_step_start(i, total_steps, STEP_NAMES[step], cmd)
        success = run_cmd(cmd)

        if not success:
            print_step_failure(build_resume_cmd(args, step))
            sys.exit(1)

        # Show stats based on step
        stats = get_db_stats(config['db_path'])

        if step == "reviews":
            print_step_success([
                f"Reviews: {stats.get('reviews', 0):,}",
                f"Saved to: {config['db_path']}",
            ])

        elif step == "trust":
            print_step_success([
                f"Trust scores: {stats.get('trust_total', 0):,}",
                f"Burst flagged: {stats.get('burst_flagged', 0):,}",
                f"Trusted (>=0.3): {stats.get('trusted', 0):,}",
                f"Saved to: {config['db_path']}",
            ])

        elif step == "embeddings":
            chroma_count = get_chroma_count(config['chroma_path'])
            print_step_success([
                f"Vectors: {chroma_count:,}",
                f"Saved to: {config['chroma_path']}/",
            ])

    # Final summary
    elapsed = (datetime.now() - start).total_seconds()
    stats = get_db_stats(config['db_path'])
    chroma_count = get_chroma_count(config['chroma_path'])
    print_summary(config, args.test, elapsed, stats, chroma_count)

    if args.test and not args.no_cleanup:
        cleanup(config)


if __name__ == "__main__":
    main()
