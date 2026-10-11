"""kcal fetch: daily steps and workout calories from Garmin Connect."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from kcal.auth import garmin_profile_id, login
from kcal.daily import fetch_days, resolve_days
from kcal.models import DayStats


def _render_csv(stats: list[DayStats]) -> str:
    from io import StringIO

    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "date",
            "weight_kg",
            "workout_active_calories",
            "workout_calories",
            "steps",
            "non_workout_steps",
            "active_calories",
            "passive_calories",
        ]
    )
    for s in stats:
        writer.writerow(
            [
                s.date,
                s.weight_kg if s.weight_kg is not None else "",
                round(s.workout_active_calories),
                round(s.workout_calories),
                s.total_steps,
                s.non_workout_steps,
                round(s.active_calories),
                round(s.bmr_calories),
            ]
        )
    return buf.getvalue()


def _cmd_fetch(args: argparse.Namespace) -> int:
    try:
        days = resolve_days(args.date, args.from_date, args.to_date)
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    stats = fetch_days(days)

    output = _render_csv(stats)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8", newline="")
    else:
        print(output, end="")
    return 0


def _cmd_whoami(args: argparse.Namespace) -> int:
    api = login()
    print(f"profile ID:   {garmin_profile_id(api)}")
    print(f"display name: {getattr(api, 'display_name', None) or ''}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kcal")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser(
        "fetch", help="Fetch steps and workout calories for a day or date range"
    )
    fetch.add_argument("--date", help="Single day, YYYY-MM-DD")
    fetch.add_argument(
        "--from",
        dest="from_date",
        metavar="DATE",
        help="Range start, YYYY-MM-DD",
    )
    fetch.add_argument(
        "--to",
        dest="to_date",
        metavar="DATE",
        help="Range end, YYYY-MM-DD (default: yesterday, requires --from)",
    )
    fetch.add_argument("--output", help="Write to this file instead of stdout")
    fetch.set_defaults(func=_cmd_fetch)

    whoami = subparsers.add_parser(
        "whoami", help="Show the logged-in Garmin account (its profile ID is KCAL_GARMIN_OWNER)"
    )
    whoami.set_defaults(func=_cmd_whoami)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
