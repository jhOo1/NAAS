import unittest

from smoke import CASES, POLICIES, PROFILES, branch, choose_sensor, evaluate, optimal


class SensingTest(unittest.TestCase):
    def test_bayesian_update_conserves_prior(self):
        case = CASES[0]
        branches = [branch(case.prior, case.sensors[0].positive, observed)
                    for observed in (False, True)]
        for state, prior in enumerate(case.prior):
            self.assertAlmostEqual(sum(p * posterior[state] for p, posterior in branches), prior)

    def test_zero_budget_matches_stop_for_all_policies(self):
        for case in CASES:
            for profile in PROFILES:
                for policy in POLICIES:
                    self.assertEqual(evaluate(case, profile, policy, 0, "matched"),
                                     evaluate(case, profile, "stop", 0, "matched"))

    def test_two_step_value_matches_execution_with_correct_model(self):
        for case in CASES:
            for profile in PROFILES:
                for budget in (0.0, 0.2, 0.45, 0.8):
                    with self.subTest(case=case.name, profile=profile.name, budget=budget):
                        _, planned = optimal(case.prior, profile, case.sensors, budget,
                                             case.deadline, 2, "matched")
                        actual = evaluate(case, profile, "voi_2", budget, "matched").net
                        self.assertAlmostEqual(planned, actual)
                        for policy in POLICIES:
                            self.assertGreaterEqual(actual + 1e-9,
                                                    evaluate(case, profile, policy, budget, "matched").net)

    def test_do_not_acquire_after_deadline(self):
        case = CASES[-1]
        for profile in PROFILES:
            for policy in POLICIES:
                self.assertIsNone(choose_sensor(policy, case.prior, profile, case.sensors,
                                                0.8, 0, 2, "matched"))

    def test_same_context_can_lead_to_different_observations(self):
        case = CASES[1]
        picked = [choose_sensor("voi_2", case.prior, profile, case.sensors, 0.8,
                                case.deadline, 2, "matched").name for profile in PROFILES]
        self.assertEqual(picked, ["artifact", "status"])

    def test_shifted_model_can_reverse_the_lookahead_advantage(self):
        benefits = {
            model: sum(evaluate(case, profile, "voi_2", 0.8, model).net
                       - evaluate(case, profile, "voi_1", 0.8, model).net
                       for case in CASES for profile in PROFILES)
            for model in ("matched", "shifted")
        }
        self.assertGreater(benefits["matched"], 0)
        self.assertLess(benefits["shifted"], 0)
