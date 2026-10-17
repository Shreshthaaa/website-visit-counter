from __future__ import annotations

import hashlib
from bisect import bisect_left, insort


class ConsistentHash:
    """Maps keys to nodes on a hash ring.

    Each node gets `virtual_nodes` points on the ring so keys spread evenly,
    and adding or removing a node only moves the keys that node owned.
    """

    def __init__(self, nodes: list[str] | None = None, virtual_nodes: int = 100):
        self.virtual_nodes = virtual_nodes
        self.hash_ring: dict[int, str] = {}
        self.sorted_keys: list[int] = []

        for node in nodes or []:
            self.add_node(node)

    def _hash(self, key: str) -> int:
        # Not used for security. Changing the hash moves every key, so it
        # would need a data migration.
        return int(hashlib.md5(key.encode(), usedforsecurity=False).hexdigest(), 16)

    def add_node(self, node: str) -> None:
        for i in range(self.virtual_nodes):
            hash_val = self._hash(f"{node}#{i}")
            if hash_val not in self.hash_ring:
                insort(self.sorted_keys, hash_val)
                self.hash_ring[hash_val] = node

    def remove_node(self, node: str) -> None:
        for i in range(self.virtual_nodes):
            hash_val = self._hash(f"{node}#{i}")
            if self.hash_ring.get(hash_val) != node:
                continue
            del self.hash_ring[hash_val]
            del self.sorted_keys[bisect_left(self.sorted_keys, hash_val)]

    def get_node(self, key: str) -> str | None:
        """First node clockwise from the key's hash, wrapping around."""
        if not self.hash_ring:
            return None

        index = bisect_left(self.sorted_keys, self._hash(key))
        if index == len(self.sorted_keys):
            index = 0

        return self.hash_ring[self.sorted_keys[index]]

    @property
    def nodes(self) -> list[str]:
        return sorted(set(self.hash_ring.values()))
