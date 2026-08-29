"""Chart grounding gate: every plotted number (value + CI bounds) must appear in its cited finding, or
the whole chart is dropped (fail-safe). The safety boundary — a fabricated bar must never ship."""
from roster_kernel.research.react import ChartBar, ChartSpec, VerifiedClaim, _validate_charts

V = [
    VerifiedClaim(text="Drug A had a 53% response rate", atom_id="a1", quote="53% response rate"),
    VerifiedClaim(text="Drug B response was 41%", atom_id="a2", quote="41%"),
    VerifiedClaim(text="HR 0.78 (95% CI 0.65-0.94) for the primary outcome", atom_id="a3",
                  quote="HR 0.78 (95% CI 0.65-0.94)"),
    VerifiedClaim(text="the comparator HR was 0.91 (95% CI 0.80 to 1.05)", atom_id="a4",
                  quote="0.91 (95% CI 0.80 to 1.05)"),
]


def _bar(**kw):
    return ChartBar(**kw)


def test_grounded_bar_chart_survives():
    ch = ChartSpec(kind="bar", title="Response", unit="%", bars=[
        _bar(label="Drug A", value=53, value_str="53%", finding=1),
        _bar(label="Drug B", value=41, value_str="41%", finding=2)])
    out = _validate_charts([ch], V)
    assert len(out) == 1 and out[0]["kind"] == "bar" and len(out[0]["bars"]) == 2


def test_one_ungrounded_bar_drops_the_whole_chart():
    ch = ChartSpec(bars=[_bar(label="A", value=53, value_str="53%", finding=1),
                         _bar(label="C", value=88, value_str="88%", finding=2)])  # 88% in no finding
    assert _validate_charts([ch], V) == []


def test_bad_finding_index_drops_chart():
    ch = ChartSpec(bars=[_bar(label="A", value=53, value_str="53%", finding=1),
                         _bar(label="B", value=41, value_str="41%", finding=9)])
    assert _validate_charts([ch], V) == []


def test_interval_chart_requires_grounded_bounds():
    # both estimate AND CI bounds present verbatim → survives
    ch = ChartSpec(kind="interval", title="HR", bars=[
        _bar(label="Trial 1", value=0.78, value_str="0.78", low=0.65, low_str="0.65",
             high=0.94, high_str="0.94", finding=3),
        _bar(label="Trial 2", value=0.91, value_str="0.91", low=0.80, low_str="0.80",
             high=1.05, high_str="1.05", finding=4)])
    assert len(_validate_charts([ch], V)) == 1


def test_interval_with_fabricated_ci_bound_drops_chart():
    ch = ChartSpec(kind="interval", bars=[
        _bar(label="Trial 1", value=0.78, value_str="0.78", low=0.65, low_str="0.65",
             high=0.99, high_str="0.99", finding=3),   # 0.99 not in finding 3
        _bar(label="Trial 2", value=0.91, value_str="0.91", finding=4)])
    assert _validate_charts([ch], V) == []


def test_grouped_bar_all_values_grounded():
    v = [VerifiedClaim(text="Drug A: response 53%, serious AE 7%", atom_id="a", quote="response 53%, serious AE 7%"),
         VerifiedClaim(text="Drug B: response 41%, serious AE 3%", atom_id="b", quote="response 41%, serious AE 3%")]
    ch = ChartSpec(kind="grouped_bar", title="Benefit vs risk", unit="%", bars=[
        _bar(label="Drug A", series="Response", value=53, value_str="53%", finding=1),
        _bar(label="Drug A", series="Serious AE", value=7, value_str="7%", finding=1),
        _bar(label="Drug B", series="Response", value=41, value_str="41%", finding=2),
        _bar(label="Drug B", series="Serious AE", value=3, value_str="3%", finding=2)])
    assert len(_validate_charts([ch], v)) == 1


def test_single_group_is_not_a_chart():
    ch = ChartSpec(bars=[_bar(label="A", value=53, value_str="53%", finding=1)])
    assert _validate_charts([ch], V) == []


def test_empty_is_noop():
    assert _validate_charts([], V) == []


# ---- line charts: a trend needs >=3 grounded points per series, <=3 series ----

VL = [
    VerifiedClaim(text="HbA1c fell from 8.2% at baseline to 7.1% at month 3 and 6.8% at month 6",
                  atom_id="l1", quote="8.2% at baseline to 7.1% at month 3 and 6.8% at month 6"),
    VerifiedClaim(text="placebo HbA1c was 8.1% at baseline, 8.0% at month 3, 7.9% at month 6",
                  atom_id="l2", quote="8.1% at baseline, 8.0% at month 3, 7.9% at month 6"),
]


def _line_points(series, vals, finding):
    return [_bar(label=lb, value=v, value_str=s, series=series, finding=finding)
            for lb, v, s in vals]


def test_grounded_line_chart_survives():
    ch = ChartSpec(kind="line", title="HbA1c over time", unit="%", bars=_line_points(
        "", [("Baseline", 8.2, "8.2%"), ("Month 3", 7.1, "7.1%"), ("Month 6", 6.8, "6.8%")], 1))
    out = _validate_charts([ch], VL)
    assert len(out) == 1 and out[0]["kind"] == "line" and len(out[0]["bars"]) == 3


def test_multi_series_line_chart_survives():
    ch = ChartSpec(kind="line", title="HbA1c: drug vs placebo", unit="%", bars=(
        _line_points("Drug", [("Baseline", 8.2, "8.2%"), ("Month 3", 7.1, "7.1%"),
                              ("Month 6", 6.8, "6.8%")], 1)
        + _line_points("Placebo", [("Baseline", 8.1, "8.1%"), ("Month 3", 8.0, "8.0%"),
                                   ("Month 6", 7.9, "7.9%")], 2)))
    assert len(_validate_charts([ch], VL)) == 1


def test_line_with_two_points_is_dropped():
    ch = ChartSpec(kind="line", bars=_line_points(
        "", [("Baseline", 8.2, "8.2%"), ("Month 3", 7.1, "7.1%")], 1))
    assert _validate_charts([ch], VL) == []


def test_line_with_one_short_series_is_dropped():
    # first series has 3 points, second only 2 → the whole chart drops (every series needs a trend)
    ch = ChartSpec(kind="line", bars=(
        _line_points("Drug", [("Baseline", 8.2, "8.2%"), ("Month 3", 7.1, "7.1%"),
                              ("Month 6", 6.8, "6.8%")], 1)
        + _line_points("Placebo", [("Baseline", 8.1, "8.1%"), ("Month 3", 8.0, "8.0%")], 2)))
    assert _validate_charts([ch], VL) == []


def test_line_with_four_series_is_dropped():
    bars = []
    for s in ("S1", "S2", "S3", "S4"):
        bars += _line_points(s, [("Baseline", 8.2, "8.2%"), ("Month 3", 7.1, "7.1%"),
                                 ("Month 6", 6.8, "6.8%")], 1)
    assert _validate_charts([ChartSpec(kind="line", bars=bars)], VL) == []


def test_line_with_unverified_point_is_dropped():
    ch = ChartSpec(kind="line", bars=_line_points(
        "", [("Baseline", 8.2, "8.2%"), ("Month 3", 7.1, "7.1%"), ("Month 6", 6.5, "6.5%")], 1))
    assert _validate_charts([ch], VL) == []  # 6.5% appears in no finding


# ---- pie charts: parts of a whole — 2-6 slices, none negative, all grounded ----

VP = [VerifiedClaim(text="of the isolates, 62% were type A, 27% type B, and 11% other",
                    atom_id="p1", quote="62% were type A, 27% type B, and 11% other")]


def test_grounded_pie_chart_survives():
    ch = ChartSpec(kind="pie", title="Isolate distribution", unit="%", bars=[
        _bar(label="Type A", value=62, value_str="62%", finding=1),
        _bar(label="Type B", value=27, value_str="27%", finding=1),
        _bar(label="Other", value=11, value_str="11%", finding=1)])
    out = _validate_charts([ch], VP)
    assert len(out) == 1 and out[0]["kind"] == "pie" and len(out[0]["bars"]) == 3


def test_pie_with_unverified_slice_is_dropped():
    ch = ChartSpec(kind="pie", bars=[
        _bar(label="Type A", value=62, value_str="62%", finding=1),
        _bar(label="Type B", value=38, value_str="38%", finding=1)])  # 38% not in the finding
    assert _validate_charts([ch], VP) == []


def test_pie_with_negative_slice_is_dropped():
    v = [VerifiedClaim(text="the change was -5% in one group and 62% in the rest",
                       atom_id="n", quote="-5% in one group and 62%")]
    ch = ChartSpec(kind="pie", bars=[
        _bar(label="A", value=-5, value_str="5%", finding=1),
        _bar(label="B", value=62, value_str="62%", finding=1)])
    assert _validate_charts([ch], v) == []


def test_pie_with_seven_slices_is_dropped():
    v = [VerifiedClaim(text="shares were 10% 11% 12% 13% 14% 15% 25%", atom_id="m",
                       quote="10% 11% 12% 13% 14% 15% 25%")]
    bars = [_bar(label=f"S{i}", value=p, value_str=f"{p}%", finding=1)
            for i, p in enumerate((10, 11, 12, 13, 14, 15, 25))]
    assert _validate_charts([ChartSpec(kind="pie", bars=bars)], v) == []


def test_pie_slice_count_bounds_inclusive():
    # exactly 2 and exactly 6 slices are both legal
    v = [VerifiedClaim(text="shares were 10% 11% 12% 13% 14% 40% and also 60%", atom_id="b",
                       quote="10% 11% 12% 13% 14% 40% and also 60%")]
    two = ChartSpec(kind="pie", bars=[_bar(label="A", value=40, value_str="40%", finding=1),
                                      _bar(label="B", value=60, value_str="60%", finding=1)])
    six = ChartSpec(kind="pie", bars=[
        _bar(label=f"S{i}", value=p, value_str=f"{p}%", finding=1)
        for i, p in enumerate((10, 11, 12, 13, 14, 40))])
    assert len(_validate_charts([two, six], v)) == 2
