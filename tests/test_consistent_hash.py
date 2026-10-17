from app.core.consistent_hash import ConsistentHash

NODE_A = "redis://redis1:7070"
NODE_B = "redis://redis2:7071"


def test_empty_ring_returns_none():
    assert ConsistentHash().get_node("anything") is None


def test_lookup_is_deterministic():
    ring = ConsistentHash([NODE_A, NODE_B])
    first = ring.get_node("page-42")
    assert all(ring.get_node("page-42") == first for _ in range(50))


def test_default_nodes_are_not_shared_between_instances():
    first = ConsistentHash()
    first.add_node(NODE_A)
    assert ConsistentHash().get_node("page-1") is None


def test_keys_spread_across_both_shards():
    ring = ConsistentHash([NODE_A, NODE_B], virtual_nodes=100)
    placements = [ring.get_node(f"page-{i}") for i in range(1000)]

    share_a = placements.count(NODE_A) / len(placements)
    assert 0.35 < share_a < 0.65


def test_removing_a_node_only_moves_that_nodes_keys():
    ring = ConsistentHash([NODE_A, NODE_B])
    keys = [f"page-{i}" for i in range(500)]
    before = {key: ring.get_node(key) for key in keys}

    ring.remove_node(NODE_B)
    after = {key: ring.get_node(key) for key in keys}

    for key in keys:
        if before[key] == NODE_A:
            assert after[key] == NODE_A, "keys on the surviving shard must not move"
        else:
            assert after[key] == NODE_A, "orphaned keys must land on the survivor"


def test_adding_a_node_moves_a_minority_of_keys():
    ring = ConsistentHash([NODE_A])
    keys = [f"page-{i}" for i in range(1000)]
    before = {key: ring.get_node(key) for key in keys}

    ring.add_node(NODE_B)
    moved = sum(1 for key in keys if ring.get_node(key) != before[key])
    assert 0.2 < moved / len(keys) < 0.8
