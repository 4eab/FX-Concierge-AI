#!/usr/bin/env python3
"""
Database Reset Script — fx-monitor-ai
Safely truncates all tables to simulate a first-time user experience.
Run: python scripts/reset_db.py [--confirm]
"""
import argparse
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def reset_database(dry_run: bool = True) -> None:
    from app.database import SessionLocal
    from app.models import UserProfile, DailyRate, HistoricalStats, RateScore

    tables = [
        ("rate_scores", RateScore),
        ("historical_stats", HistoricalStats),
        ("daily_rates", DailyRate),
        ("user_profiles", UserProfile),
    ]

    print("=== FX Monitor AI — Database Reset ===")

    with SessionLocal() as db:
        for table_name, model in tables:
            count = db.query(model).count()
            print(f"  {table_name}: {count} rows")

    if dry_run:
        print("\n[DRY RUN] No changes made. Pass --confirm to actually reset.")
        return

    print("\n⚠️  Deleting all data...\n")

    with SessionLocal() as db:
        for table_name, model in tables:
            deleted = db.query(model).delete()
            print(f"  ✓ Deleted {deleted} rows from {table_name}")
        db.commit()

    print("\n✅ Database reset complete. All tables are empty.")
    print("   The schema is preserved — run the bot to start fresh onboarding.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset fx-monitor-ai database")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete data (default is dry-run)",
    )
    args = parser.parse_args()
    reset_database(dry_run=not args.confirm)
