"""Unit tests for the validation-tier decision (hooks/validate_tier.py)."""

import validate_tier as vt


def test_skip_tier_for_docs_only():
    r = vt.recommend_tier(["README.md", "docs/guide.md"], {}, 12)
    assert r["tier"] == "Skip"


def test_light_tier_for_small_code_change():
    r = vt.recommend_tier(["src/util.py"], {"src/util.py": ["x = 1"]}, 10)
    assert r["tier"] == "Light"


def test_full_tier_above_loc_threshold():
    r = vt.recommend_tier(["src/util.py"], {}, vt.LIGHT_THRESHOLD + 1)
    assert r["tier"] == "Full"


def test_full_tier_for_sensitive_path():
    r = vt.recommend_tier(["src/auth/login.py"], {}, 4)
    assert r["tier"] == "Full"
    assert r["sensitive_patterns"]  # non-empty


def test_block_on_forbidden_marker():
    r = vt.recommend_tier(["src/app.py"], {"src/app.py": ["run()  # TODO later"]}, 3)
    assert r["tier"] == "BLOCK"
    assert any("TODO" in m for m in r["markers"])


def test_marker_in_docs_is_not_blocked():
    # forbidden markers in docs/config are prose, not leftover code markers —
    # they must NOT block (the scanner skips skippable files).
    r = vt.recommend_tier(["README.md"], {"README.md": ["# FIXME later"]}, 1)
    assert r["tier"] == "Skip"


def test_stable_return_schema():
    r = vt.recommend_tier(["src/util.py"], {}, 5)
    for key in ("loc", "files", "markers", "sensitive_patterns", "tier", "reason"):
        assert key in r


def test_all_files_skippable():
    assert vt.all_files_skippable(["README.md", "config.yaml", "x.json"]) is True
    assert vt.all_files_skippable(["src/app.py"]) is False
    assert vt.all_files_skippable([]) is True


def test_matched_sensitive_paths():
    assert "payment" in vt.matched_sensitive_paths(["src/payment/stripe.py"])
    assert vt.matched_sensitive_paths(["src/helpers.py"]) == []


def test_real_env_is_sensitive_but_example_is_not():
    assert vt.matched_sensitive_paths([".env"])  # real .env escalates
    assert vt.matched_sensitive_paths([".env.example"]) == []  # template does not
