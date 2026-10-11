from dataclasses import replace
from datetime import date

import pytest

from kcal import cli
from kcal.cli import _render_csv
from kcal.models import DayStats, Workout


ONE_DAY = [
    DayStats(
        date="2026-07-06",
        weight_kg=70.5,
        total_steps=10000,
        total_calories=2400,
        active_calories=600,
        bmr_calories=1800,
        workouts=[
            Workout(
                activity_id=1,
                name="Morning Run",
                activity_type="running",
                start_time="2026-07-06 07:00:00",
                duration_seconds=1800,
                calories=350,
                bmr_calories=50,
                steps=4000,
            )
        ],
    )
]


def test_render_csv_header_and_row():
    rows = _render_csv(ONE_DAY).splitlines()
    assert rows[0] == (
        "date,weight_kg,workout_active_calories,workout_calories,steps,"
        "non_workout_steps,active_calories,passive_calories"
    )
    assert rows[1] == "2026-07-06,70.5,300,350,10000,6000,600,1800"


def test_render_csv_blank_weight_when_no_weigh_in():
    day = replace(ONE_DAY[0], weight_kg=None)
    rows = _render_csv([day]).splitlines()
    assert rows[1] == "2026-07-06,,300,350,10000,6000,600,1800"


@pytest.mark.parametrize("to_file", [False, True])
def test_fetch_command_writes_csv(monkeypatch, tmp_path, capsys, to_file):
    calls = []

    def fetch(days):
        calls.append(days)
        return ONE_DAY

    monkeypatch.setattr(cli, "fetch_days", fetch)
    output = tmp_path / "day.csv"
    args = ["fetch", "--date", "2026-07-06"]
    if to_file:
        args += ["--output", str(output)]
    assert cli.main(args) == 0
    assert calls == [[date(2026, 7, 6)]]
    captured = capsys.readouterr()
    assert captured.err == ""
    expected = (
        "date,weight_kg,workout_active_calories,workout_calories,steps,non_workout_steps,active_calories,passive_calories\r\n"
        "2026-07-06,70.5,300,350,10000,6000,600,1800\r\n"
    )
    if to_file:
        assert captured.out == ""
        assert output.read_bytes() == expected.encode("utf-8")
    else:
        assert not output.exists()
        assert captured.out == expected


def test_invalid_fetch_date_does_not_log_in_or_overwrite_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "fetch_days", lambda days: pytest.fail("fetched invalid dates"))
    output = tmp_path / "existing.csv"
    output.write_text("keep me", encoding="utf-8")
    assert cli.main(["fetch", "--date", "2026-02-30", "--output", str(output)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Invalid date" in captured.err
    assert output.read_text(encoding="utf-8") == "keep me"
