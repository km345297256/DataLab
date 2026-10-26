from app.application.services.visualization_result_cache import VisualizationResultCache


def test_private_entries_have_hard_expiry_without_sliding_on_reads():
    now = [0]
    cache = VisualizationResultCache(ttl=60, clock=lambda: now[0])
    cache.put("a", "key", b"derived")
    assert cache.get("b", "key") is None
    now[0] = 59
    assert cache.get("a", "key") == b"derived"
    now[0] = 60
    assert cache.get("a", "key") is None and cache.size_bytes == 0


def test_byte_and_count_limits_evict_least_recent_entries():
    cache = VisualizationResultCache(max_bytes=9, max_entry_bytes=4, max_entries=2)
    cache.put("a", "1", b"1234")
    cache.put("b", "2", b"5678")
    cache.get("a", "1")
    cache.put("c", "3", b"90")
    assert cache.get("b", "2") is None
    assert cache.get("a", "1") == b"1234"
    assert cache.size_bytes == 6
    cache.put("c", "too-big", b"12345")
    assert cache.size_bytes == 6


def test_one_user_evicts_own_results_before_other_users():
    cache = VisualizationResultCache(per_user_entries=2, per_user_bytes=5)
    cache.put("b", "1", b"bbb")
    cache.put("a", "1", b"aa")
    cache.put("a", "2", b"aa")
    cache.put("a", "3", b"aaa")
    assert cache.get("b", "1") == b"bbb"
    assert cache.get("a", "1") is None
    assert cache.get("a", "2") == b"aa"
    assert cache.size_bytes == 8


def test_replacing_an_entry_does_not_leak_budget_and_missing_principal_is_not_cached():
    cache = VisualizationResultCache()
    cache.put("a", "key", b"1234")
    cache.put("a", "key", b"1")
    cache.put(None, "key", b"secret")
    cache.put("a", None, b"secret")
    assert cache.size_bytes == 1
    assert cache.get(None, "key") is None


def test_key_is_order_independent_but_does_not_coerce_option_types():
    key = VisualizationResultCache.key
    assert key({"row": 1, "column": 2}) == key({"column": 2, "row": 1})
    assert key({"row": 1}) != key({"row": True}) != key({"row": "1"})
    assert key({"row": float("nan")}) is None
    assert key("x" * 65536) is None
