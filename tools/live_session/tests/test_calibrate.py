from tools.live_session.calibrate import latency_profile, semaphore, separating_floor, token_margin


def test_the_margin_is_the_smallest_that_covers_every_sample_plus_headroom():
    ratios = {
        "a:x": {"observed": 110, "platform_raw": 100},
        "a:y": {"observed": 52, "platform_raw": 50},
        "b:x": {"observed": 90, "platform_raw": 100},
    }
    r = token_margin(ratios)
    assert (
        r["covers_all"] and r["minimal_margin"] <= 0.101 and 0.12 <= r["margin"] <= 0.13
    )  # 10 % needed + 2 pp, rounded up to a whole percent
    assert (
        token_margin({"a": {"observed": 80, "platform_raw": 100}}, headroom_pp=2.0)["margin"] == 0.02
    )  # the platform already over-counts: only the headroom remains
    assert token_margin({"a": {"observed": 100, "platform_raw": 100}}, headroom_pp=0)["covers_all"]


def test_the_floor_separates_related_from_unrelated_and_rounds_down():
    r = separating_floor([0.95, 0.97, 0.99], [0.80, 0.86, 0.91])
    assert r["separable"] and r["accuracy"] == 1.0 and 0.91 <= r["floor"] <= 0.95
    overlap = separating_floor([0.90, 0.95, 0.97], [0.80, 0.92, 0.85])
    assert not overlap["separable"] and 0.0 < overlap["accuracy"] < 1.0
    assert separating_floor([0.5555], [0.1])["floor"] == 0.32  # (0.5555 + 0.1) / 2 = 0.32775 -> 0.32


def test_semaphore_and_latency_profile():
    assert semaphore({"recommended_semaphore_limit": 6})["semaphore_limit"] == 6
    assert semaphore({})["semaphore_limit"] == 1
    p = latency_profile({"chat": {"oso/general": [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000], "oso/fast": [1, 2]}})
    assert p["chat"]["oso/general"] == {"p50_ms": 550, "p95_ms": 1000} and "oso/fast" not in p["chat"]


def test_the_proposal_changes_only_thresholds_statuses_and_the_latency_profile(tmp_path, monkeypatch):
    import json

    from tools.live_session import __main__ as ls

    monkeypatch.setattr(ls, "REPORTS", tmp_path)
    d = ls.out_dir()
    (d / "a.json").write_text(
        json.dumps(
            {
                "exit_code": 0,
                "spike_results": {
                    "live-TK-02": {"evidence": {"ratios": {"x:y": {"observed": 105, "platform_raw": 100}}}},
                    "live-GR-13": {"evidence": {"recommended_semaphore_limit": 6}},
                },
            }
        )
    )
    (d / "b.json").write_text(
        json.dumps({"exit_code": 0, "report": {"embeddings": {"related": {"cosine": [0.96, 0.98]}, "unrelated": {"cosine": [0.84, 0.9]}}}})
    )
    cal = ls.calibrations(d)
    assert (
        cal["SEMAPHORE_LIMIT"]["semaphore_limit"] == 6
        and cal["CONTEXT_TOKEN_MARGIN"]["covers_all"]
        and 0.9 <= cal["IDENTITY_COS_MIN"]["floor"] <= 0.96
    )
    files = ls.proposed_files(cal)
    assert "SEMAPHORE_LIMIT=6" in files[".env.example"] and "status: accepted" in files["compat/model-purposes.yaml"]
    assert "Accepted (live session" in files["docs/adr/ADR-012-model-gateway-and-router-contract.md"]
    assert ls.set_env("A=1\n", "B", "2") == "A=1\nB=2\n" and ls.set_env("A=1\n", "A", "3") == "A=3\n"
