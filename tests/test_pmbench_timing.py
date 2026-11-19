import unittest

from run_pmbench_timing import ARMS, BUDGET, blocks, read_reason


class TimingBudgetTest(unittest.TestCase):
    def test_each_timing_policy_uses_exactly_one_read_per_block(self):
        schedule = blocks(80)
        self.assertEqual(list(range(1, 81)), [index for block in schedule
                                               for index in range(block["first"], block["last"] + 1)])
        self.assertEqual(BUDGET, len(schedule))
        for arm in ARMS:
            for wants in (False, True):
                for block in schedule:
                    with self.subTest(arm=arm, wants=wants, block=block):
                        reads = []
                        for index in range(block["first"], block["last"] + 1):
                            if read_reason(arm, index, block, bool(reads), wants):
                                reads.append(index)
                        fixed_step = (block["first"] + block["last"] + 1) // 2
                        expected = fixed_step if arm == "fixed" else block["first" if wants else "last"]
                        self.assertEqual([expected], reads)

    def test_demand_timing_can_wait_for_an_interior_event(self):
        block = {"first": 9, "last": 17}
        self.assertIsNone(read_reason("on_demand", 9, block, False, False))
        self.assertEqual("requested", read_reason("on_demand", 11, block, False, True))
        self.assertIsNone(read_reason("on_demand", 12, block, True, True))


if __name__ == "__main__":
    unittest.main()
