from garmin_reader.dedupe import build_day_stats, build_weight_row

SUMMARY = {
    "totalSteps": 10000,
    "totalKilocalories": 2400,
    "activeKilocalories": 600,
    "bmrKilocalories": 1800,
}

RUN_ACTIVITY = {
    "activityId": 1,
    "activityName": "Morning Run",
    "activityType": {"typeKey": "running"},
    "startTimeLocal": "2026-07-06 07:00:00",
    "duration": 1800,
    "calories": 350,
    "bmrCalories": 50,
    "steps": 4000,
}

BIKE_ACTIVITY = {
    "activityId": 2,
    "activityName": "Evening Ride",
    "activityType": {"typeKey": "cycling"},
    "startTimeLocal": "2026-07-06 18:00:00",
    "duration": 3600,
    "calories": 500,
    "bmrCalories": 80,
    "steps": None,
}


def test_no_workouts_leaves_steps_untouched():
    stats = build_day_stats("2026-07-06", SUMMARY, [])
    assert stats.total_steps == 10000
    assert stats.workout_steps == 0
    assert stats.non_workout_steps == 10000
    assert stats.workout_calories == 0


def test_step_based_workout_is_deducted_from_total():
    stats = build_day_stats("2026-07-06", SUMMARY, [RUN_ACTIVITY])
    assert stats.workout_steps == 4000
    assert stats.non_workout_steps == 6000
    assert stats.workout_calories == 350
    assert stats.workout_active_calories == 300  # 350 - 50 bmrCalories
    assert stats.non_workout_active_calories == 300  # 600 - 300


def test_non_step_workout_contributes_zero_steps():
    stats = build_day_stats("2026-07-06", SUMMARY, [BIKE_ACTIVITY])
    assert stats.workout_steps == 0
    assert stats.non_workout_steps == 10000
    assert stats.workout_calories == 500


def test_multiple_workouts_are_summed():
    stats = build_day_stats("2026-07-06", SUMMARY, [RUN_ACTIVITY, BIKE_ACTIVITY])
    assert stats.workout_steps == 4000
    assert stats.non_workout_steps == 6000
    assert stats.workout_calories == 850
    assert len(stats.workouts) == 2


def test_non_workout_steps_never_negative():
    summary = {**SUMMARY, "totalSteps": 1000}
    stats = build_day_stats("2026-07-06", summary, [RUN_ACTIVITY])
    assert stats.non_workout_steps == 0


def test_workout_active_calories_nets_out_bmr():
    """Garmin's per-activity `calories` is gross - it includes the BMR
    calories for the activity's duration (`bmrCalories`), which Garmin
    separately counts in the day's bmr_calories, not active_calories. So a
    workout's gross calories can exceed the day's active_calories even
    though its *net* active contribution can't.
    """
    stats = build_day_stats("2026-07-06", SUMMARY, [RUN_ACTIVITY])
    workout = stats.workouts[0]
    assert workout.active_calories == 300  # 350 - 50


def test_non_workout_active_calories_never_negative():
    summary = {**SUMMARY, "activeKilocalories": 100}
    stats = build_day_stats("2026-07-06", summary, [RUN_ACTIVITY])
    assert stats.workout_active_calories == 300
    assert stats.non_workout_active_calories == 0


def test_weight_kg_none_when_no_weigh_in_given():
    stats = build_day_stats("2026-07-06", SUMMARY, [])
    assert stats.weight_kg is None


def test_weight_kg_none_when_weigh_in_list_empty():
    stats = build_day_stats("2026-07-06", SUMMARY, [], {"dateWeightList": []})
    assert stats.weight_kg is None


def test_weight_kg_converts_grams_to_kg():
    weigh_in = {"dateWeightList": [{"date": 1751792400000, "weight": 70500.0}]}
    stats = build_day_stats("2026-07-06", SUMMARY, [], weigh_in)
    assert stats.weight_kg == 70.5


def test_weight_kg_takes_last_weigh_in_of_day():
    weigh_in = {
        "dateWeightList": [
            {"date": 1751792400000, "weight": 71000.0},  # earlier
            {"date": 1751835600000, "weight": 70200.0},  # later - wins
        ]
    }
    stats = build_day_stats("2026-07-06", SUMMARY, [], weigh_in)
    assert stats.weight_kg == 70.2


def test_workout_active_calories_never_negative():
    """bmrCalories can exceed calories in malformed/incomplete source data
    (e.g. calories missing but bmrCalories present) - active_calories must
    still floor at 0 rather than go negative.
    """
    activity = {**RUN_ACTIVITY, "calories": None, "bmrCalories": 50}
    stats = build_day_stats("2026-07-06", SUMMARY, [activity])
    assert stats.workouts[0].active_calories == 0
    assert stats.workout_active_calories == 0


def test_weight_row_includes_body_composition_from_last_weigh_in():
    weigh_in = {
        "dateWeightList": [
            {"date": 1, "weight": 77000.0, "bodyFat": 23.0, "muscleMass": 31000},
            {"date": 2, "weight": 76550.0, "bodyFat": 22.4, "muscleMass": 31120},
        ]
    }
    assert build_weight_row("2026-10-08", weigh_in) == {
        "date": "2026-10-08",
        "weight_kg": 76.5,
        "body_fat_pct": 22.4,
        "muscle_mass_kg": 31.1,
        "count": 2,
    }


def test_weight_row_omits_unreported_fields():
    weigh_in = {"dateWeightList": [{"date": 1, "weight": 70500.0, "bodyFat": None}]}
    assert build_weight_row("2026-07-06", weigh_in) == {"date": "2026-07-06", "weight_kg": 70.5}


def test_weight_row_none_without_weigh_in():
    assert build_weight_row("2026-07-06", {"dateWeightList": []}) is None


def test_latest_weigh_in_falls_back_to_gmt_timestamp():
    weigh_in = {
        "dateWeightList": [
            {"timestampGMT": 1, "weight": 71000.0},
            {"timestampGMT": 2, "weight": 70200.0},  # later - wins
        ]
    }
    stats = build_day_stats("2026-07-06", SUMMARY, [], weigh_in)
    assert stats.weight_kg == 70.2
