"""Exact enumeration of a small, finite proactive-sensing decision problem."""

import argparse
from dataclasses import dataclass
import json
from math import log2
from typing import Tuple


Vector = Tuple[float, ...]
EPSILON = 1e-9
POLICIES = ("stop", "cheapest", "margin", "entropy", "population_voi", "voi_1", "voi_2")


@dataclass(frozen=True)
class Sensor:
    name: str
    cost: float
    ticks: int
    # P(positive | no need, help A, help B)
    positive: Vector


@dataclass(frozen=True)
class Case:
    name: str
    prior: Vector
    sensors: Tuple[Sensor, ...]
    deadline: int


@dataclass(frozen=True)
class Profile:
    name: str
    interruption: float

    def utility(self, action, state):
        if action == 0:
            return 0.0
        return (3.0 if action == state else -1.0) - self.interruption


@dataclass(frozen=True)
class Metrics:
    net: float = 0.0
    cost: float = 0.0
    correct: float = 0.0
    false_proposal: float = 0.0
    missed: float = 0.0


def expected(terms):
    return sum(weight * value for weight, value in terms)


def branch(belief, positive, observed):
    joint = tuple(p * (q if observed else 1 - q) for p, q in zip(belief, positive))
    mass = sum(joint)
    return mass, tuple(weight / mass for weight in joint) if mass else belief


def outcomes(belief, positive):
    for observed in (False, True):
        mass, posterior = branch(belief, positive, observed)
        if mass > 0:
            yield mass, posterior


def likelihood(sensor, assumed):
    # Policies share this channel. The evaluator uses the unmodified channel.
    if assumed == "matched":
        return sensor.positive
    return tuple(0.5 + (q - 0.5) * scale for q, scale in zip(sensor.positive, (0.3, 0.9, 0.45)))


def action_values(belief, profile):
    return tuple(
        expected((p, profile.utility(action, state)) for state, p in enumerate(belief))
        for action in range(len(belief))
    )


def terminal_action(belief, profile):
    values = action_values(belief, profile)
    return max(range(len(values)), key=values.__getitem__)


def available(sensors, budget, ticks):
    return tuple(s for s in sensors if s.cost <= budget + EPSILON and s.ticks <= ticks)


def remaining(sensors, chosen):
    return tuple(s for s in sensors if s != chosen)


def optimal(belief, profile, sensors, budget, ticks, depth, assumed):
    best_sensor = None
    best_value = max(action_values(belief, profile))
    if depth == 0:
        return best_sensor, best_value

    for sensor in available(sensors, budget, ticks):
        next_sensors = remaining(sensors, sensor)
        value = expected(
            (mass, optimal(posterior, profile, next_sensors, budget - sensor.cost,
                           ticks - sensor.ticks, depth - 1, assumed)[1])
            for mass, posterior in outcomes(belief, likelihood(sensor, assumed))
        ) - sensor.cost
        if value > best_value + EPSILON:
            best_sensor, best_value = sensor, value
    return best_sensor, best_value


def entropy(belief):
    return -sum(p * log2(p) for p in belief if p)


def choose_sensor(policy, belief, profile, sensors, budget, ticks, attempts, assumed):
    candidates = available(sensors, budget, ticks)
    if attempts == 0 or not candidates or policy == "stop":
        return None
    if policy == "population_voi":
        return optimal(belief, POPULATION, candidates, budget, ticks, 1, assumed)[0]
    if policy.startswith("voi_"):
        depth = 1 if policy == "voi_1" else attempts
        return optimal(belief, profile, candidates, budget, ticks, depth, assumed)[0]
    if policy == "cheapest":
        return min(candidates, key=lambda s: s.cost)

    action_scores = sorted(action_values(belief, profile), reverse=True)
    if policy == "margin" and action_scores[0] - action_scores[1] > 0.4:
        return None
    initial_entropy = entropy(belief)

    def information_per_cost(sensor):
        residual = expected((mass, entropy(posterior)) for mass, posterior in
                            outcomes(belief, likelihood(sensor, assumed)))
        return (initial_entropy - residual) / sensor.cost

    return max(candidates, key=information_per_cost)


def evaluate(case, profile, policy, budget, assumed):
    def visit(belief, true_belief, sensors, budget_left, ticks, attempts):
        sensor = choose_sensor(policy, belief, profile, sensors, budget_left, ticks,
                               attempts, assumed)
        if sensor is None:
            action = terminal_action(belief, profile)
            return Metrics(
                net=expected((p, profile.utility(action, state))
                             for state, p in enumerate(true_belief)),
                correct=true_belief[action] if action else 0.0,
                false_proposal=1.0 - true_belief[action] if action else 0.0,
                missed=sum(true_belief[1:]) - (true_belief[action] if action else 0.0),
            )

        children = []
        next_sensors = remaining(sensors, sensor)
        for observed in (False, True):
            mass, true_posterior = branch(true_belief, sensor.positive, observed)
            _, posterior = branch(belief, likelihood(sensor, assumed), observed)
            if mass > 0:
                children.append((mass, visit(posterior, true_posterior, next_sensors,
                                             budget_left - sensor.cost, ticks - sensor.ticks,
                                             attempts - 1)))
        row = {name: expected((mass, getattr(child, name)) for mass, child in children)
               for name in Metrics.__dataclass_fields__}
        row["net"] -= sensor.cost
        row["cost"] += sensor.cost
        return Metrics(**row)

    return visit(case.prior, case.prior, case.sensors, budget, case.deadline, 2)


CASES = (
    Case("uncertain_trigger", (0.62, 0.27, 0.11), (
        Sensor("status", 0.18, 1, (0.12, 0.84, 0.80)),
        Sensor("artifact", 0.12, 1, (0.49, 0.76, 0.13)),
        Sensor("full_context", 0.60, 2, (0.04, 0.97, 0.05)),
    ), 2),
    Case("uncertain_action", (0.15, 0.50, 0.35), (
        Sensor("status", 0.12, 1, (0.06, 0.89, 0.90)),
        Sensor("artifact", 0.24, 1, (0.50, 0.92, 0.07)),
        Sensor("full_context", 0.57, 2, (0.07, 0.97, 0.04)),
    ), 2),
    Case("low_opportunity", (0.87, 0.09, 0.04), (
        Sensor("status", 0.20, 1, (0.23, 0.75, 0.78)),
        Sensor("artifact", 0.14, 1, (0.48, 0.83, 0.15)),
        Sensor("full_context", 0.59, 2, (0.06, 0.96, 0.05)),
    ), 2),
    Case("strong_signal", (0.18, 0.76, 0.06), (
        Sensor("status", 0.12, 1, (0.12, 0.82, 0.85)),
        Sensor("artifact", 0.25, 1, (0.47, 0.84, 0.21)),
        Sensor("full_context", 0.56, 2, (0.06, 0.96, 0.05)),
    ), 2),
    Case("short_deadline", (0.21, 0.39, 0.40), (
        Sensor("status", 0.13, 1, (0.16, 0.76, 0.78)),
        Sensor("artifact", 0.21, 1, (0.50, 0.76, 0.24)),
        Sensor("full_context", 0.43, 2, (0.03, 0.98, 0.03)),
    ), 1),
)
PROFILES = (Profile("interruptible", 0.2), Profile("focused", 1.6))
POPULATION = Profile("population", sum(p.interruption for p in PROFILES) / len(PROFILES))


def run(budgets):
    pairs = tuple((case, profile) for case in CASES for profile in PROFILES)
    for budget in budgets:
        for assumed in ("matched", "shifted"):
            for policy in POLICIES:
                rows = [evaluate(case, profile, policy, budget, assumed)
                        for case, profile in pairs]
                yield {"budget": budget, "channel": assumed, "policy": policy, **{
                    name: sum(getattr(row, name) for row in rows) / len(rows)
                    for name in Metrics.__dataclass_fields__
                }}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=float, nargs="+", default=[0.45, 0.8])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if any(not (0 <= budget < float("inf")) for budget in args.budget):
        parser.error("budgets must be finite and nonnegative")
    rows = list(run(args.budget))
    if args.json:
        print(json.dumps(rows, indent=2, allow_nan=False))
        return
    print("budget channel policy       net    cost correct false_proposal missed")
    for row in rows:
        print("{budget:>6.2f} {channel:<7} {policy:<8} {net:>7.3f} {cost:>7.3f} "
              "{correct:>7.3f} {false_proposal:>14.3f} {missed:>6.3f}".format(**row))


if __name__ == "__main__":
    main()
