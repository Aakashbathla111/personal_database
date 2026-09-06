"""
B+ Tree Index  —  index/bplustree.py

ORDER = 4  →  each node holds at most (ORDER-1)=3 keys.
              internal nodes have at most ORDER=4 children.

Structure
---------
                    [ 40 ]
                   /       \
          [ 20 ]              [ 60 | 80 ]
         /      \            /    |     \
   [10|15]  [20|30]   [40|55]  [60|70]  [80|90]
    ↑ leaf nodes form a linked list via .next  ↑

Leaf nodes store: keys  →  list of row indices
Internal nodes   store: keys  + child pointers

Public API
----------
insert(key, row_index)
search(key)             → list of row indices  (exact match)
range_search(lo, hi)    → list of row indices  (lo <= key <= hi)
delete(key, row_index)
"""

ORDER = 4   # max children per internal node


class BPlusNode:

    def __init__(self, is_leaf=False):
        self.is_leaf  = is_leaf
        self.keys     = []
        self.children = []   # child BPlusNodes  (internal only)
        self.values   = []   # list[list[int]]   (leaf only)  — parallel to keys
        self.next     = None # next leaf         (leaf only)

    def is_full(self):
        return len(self.keys) >= ORDER - 1


class BPlusTree:

    def __init__(self):
        self.root = BPlusNode(is_leaf=True)

    # ------------------------------------------------------------------ #
    # INSERT
    # ------------------------------------------------------------------ #

    def insert(self, key, row_index: int):
        result = self._insert(self.root, key, row_index)
        if result:                          # root was split
            mid_key, new_node = result
            new_root = BPlusNode(is_leaf=False)
            new_root.keys     = [mid_key]
            new_root.children = [self.root, new_node]
            self.root = new_root

    def _insert(self, node: BPlusNode, key, row_index):
        """Returns (promoted_key, new_node) if split occurred, else None."""
        if node.is_leaf:
            self._leaf_insert(node, key, row_index)
            if node.is_full():
                # actually over-full (ORDER keys); needs split
                if len(node.keys) >= ORDER:
                    return self._split_leaf(node)
            return None
        else:
            # find child
            i = self._find_child_index(node, key)
            result = self._insert(node.children[i], key, row_index)
            if result:
                mid_key, new_child = result
                node.keys.insert(i, mid_key)
                node.children.insert(i + 1, new_child)
                if len(node.keys) >= ORDER:
                    return self._split_internal(node)
            return None

    def _leaf_insert(self, node: BPlusNode, key, row_index: int):
        i = 0
        while i < len(node.keys) and node.keys[i] < key:
            i += 1
        if i < len(node.keys) and node.keys[i] == key:
            node.values[i].append(row_index)    # duplicate key → append
        else:
            node.keys.insert(i, key)
            node.values.insert(i, [row_index])

    def _split_leaf(self, node: BPlusNode):
        mid   = len(node.keys) // 2
        new   = BPlusNode(is_leaf=True)
        new.keys   = node.keys[mid:]
        new.values = node.values[mid:]
        node.keys  = node.keys[:mid]
        node.values = node.values[:mid]
        new.next   = node.next
        node.next  = new
        return new.keys[0], new      # promote copy of first key of new leaf

    def _split_internal(self, node: BPlusNode):
        mid     = len(node.keys) // 2
        mid_key = node.keys[mid]
        new     = BPlusNode(is_leaf=False)
        new.keys     = node.keys[mid + 1:]
        new.children = node.children[mid + 1:]
        node.keys     = node.keys[:mid]
        node.children = node.children[:mid + 1]
        return mid_key, new

    # ------------------------------------------------------------------ #
    # SEARCH (exact)
    # ------------------------------------------------------------------ #

    def search(self, key) -> list[int]:
        leaf = self._find_leaf(key)
        for i, k in enumerate(leaf.keys):
            if k == key:
                return list(leaf.values[i])
        return []

    def _find_leaf(self, key) -> BPlusNode:
        node = self.root
        while not node.is_leaf:
            i = self._find_child_index(node, key)
            node = node.children[i]
        return node

    def _find_child_index(self, node: BPlusNode, key) -> int:
        i = 0
        while i < len(node.keys) and key >= node.keys[i]:
            i += 1
        return i

    # ------------------------------------------------------------------ #
    # RANGE SEARCH
    # ------------------------------------------------------------------ #

    def range_search(self, lo, hi,
                     lo_inclusive=True, hi_inclusive=True) -> list[int]:
        """
        Return all row indices where lo <= key <= hi.

        lo / hi may be None for an open-ended range:
          lo=None          → from the leftmost leaf
          hi=None          → to the rightmost leaf
          lo_inclusive=False → exclude the lo boundary (strict >)
          hi_inclusive=False → exclude the hi boundary (strict <)
        """
        if lo is None:
            leaf = self._leftmost_leaf()
        else:
            leaf = self._find_leaf(lo)

        result = []
        while leaf:
            for i, k in enumerate(leaf.keys):
                # upper-bound check
                if hi is not None:
                    if hi_inclusive and k > hi:
                        return result
                    if not hi_inclusive and k >= hi:
                        return result
                # lower-bound check
                if lo is not None:
                    if lo_inclusive and k < lo:
                        continue
                    if not lo_inclusive and k <= lo:
                        continue
                result.extend(leaf.values[i])
            leaf = leaf.next
        return result

    # ------------------------------------------------------------------ #
    # DELETE
    # ------------------------------------------------------------------ #

    def delete(self, key, row_index: int):
        self._delete_leaf(self.root, key, row_index)
        # If root became empty internal node, shrink tree
        if not self.root.is_leaf and len(self.root.keys) == 0:
            self.root = self.root.children[0]

    def _delete_leaf(self, node: BPlusNode, key, row_index: int):
        if node.is_leaf:
            for i, k in enumerate(node.keys):
                if k == key:
                    if row_index in node.values[i]:
                        node.values[i].remove(row_index)
                    if not node.values[i]:      # no more rows for this key
                        node.keys.pop(i)
                        node.values.pop(i)
                    return
        else:
            i = self._find_child_index(node, key)
            self._delete_leaf(node.children[i], key, row_index)

    # ------------------------------------------------------------------ #
    # SHIFT row indices after a physical delete
    # ------------------------------------------------------------------ #

    def rebuild_after_delete(self, deleted_row_index: int):
        """
        After a row is physically removed from the table, all stored
        row indices > deleted_row_index must be decremented by 1.
        Walk every leaf and patch the values in-place.
        """
        leaf = self._leftmost_leaf()
        while leaf:
            for i in range(len(leaf.values)):
                leaf.values[i] = [
                    (v - 1 if v > deleted_row_index else v)
                    for v in leaf.values[i]
                    if v != deleted_row_index
                ]
            leaf = leaf.next

    def _leftmost_leaf(self) -> BPlusNode:
        node = self.root
        while not node.is_leaf:
            node = node.children[0]
        return node

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def all_entries(self) -> list[tuple]:
        """Return list of (key, row_indices) from all leaves in order."""
        result = []
        leaf = self._leftmost_leaf()
        while leaf:
            for k, v in zip(leaf.keys, leaf.values):
                result.append((k, list(v)))
            leaf = leaf.next
        return result

    def __repr__(self):
        return f"BPlusTree(entries={self.all_entries()})"


# ── Self-test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    t = BPlusTree()
    for i, k in enumerate([10, 20, 5, 30, 15, 40, 25, 35, 50, 45]):
        t.insert(k, i)

    print("All entries:", t.all_entries())
    print("Search 20  :", t.search(20))
    print("Range 15-35:", t.range_search(15, 35))

    t.delete(20, 1)
    print("After del 20:", t.all_entries())
