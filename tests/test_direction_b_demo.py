import unittest

from direction_b_demo import Decision, Source, gains, run, schedule, worlds_for


class DirectionBTest(unittest.TestCase):
    def test_joint_policy_changes_source_only_for_shared_evidence(self):
        shared = run()
        independent = run(False)
        self.assertEqual(shared["joint"][:2], ("flight status", 3))
        self.assertGreater(shared["joint"][3], shared["best_single"][3])
        self.assertEqual(independent["joint"], independent["best_single"])

    def test_wrong_dependency_reverses_joint_policy_gain(self):
        results = run(False, True)
        self.assertLess(results["joint"][3], results["best_single"][3])

    def test_one_read_has_no_value_before_change_or_after_deadline(self):
        decisions = (Decision("flight", 6, 3.0, 4.0),)
        worlds = worlds_for(True)
        source = Source("flight status", (0,), 1, 0.2)
        self.assertAlmostEqual(gains(decisions, worlds, source, 2)[0], 0.0)
        self.assertGreater(gains(decisions, worlds, source, 3)[0], 0.0)
        self.assertEqual(gains(decisions, worlds, source, 6), (0.0,))
        self.assertIsNone(schedule(decisions, worlds, (source,), range(6), 0.1, sum))
