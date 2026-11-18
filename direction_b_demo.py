"""One-read diagnostic for scheduling evidence shared by multiple tasks."""

from dataclasses import dataclass
from itertools import product
from typing import Optional, Tuple


@dataclass(frozen=True)
class Decision:
    name: str
    deadline: int
    benefit: float
    false_alarm_loss: float


@dataclass(frozen=True)
class World:
    probability: float
    change_times: Tuple[Optional[int], ...]


@dataclass(frozen=True)
class Source:
    name: str
    observed_fields: Tuple[int, ...]
    latency: int
    cost: float


def changed_by(world, field, time):
    change = world.change_times[field]
    return change is not None and change <= time


def decision_value(field, decision, worlds):
    """Use unnormalized branch probabilities to compute weighted value."""
    act = sum(
        world.probability * (
            decision.benefit if changed_by(world, field, decision.deadline)
            else -decision.false_alarm_loss
        )
        for world in worlds
    )
    return max(0.0, act)


def gains(decisions, worlds, source, start):
    """Compute EVSI at each decision deadline with one common timed read."""
    finish = start + source.latency
    groups = {}
    for world in worlds:
        observation = tuple(changed_by(world, field, finish) for field in source.observed_fields)
        groups.setdefault(observation, []).append(world)

    return tuple(
        sum(decision_value(field, decision, group) for group in groups.values())
        - decision_value(field, decision, worlds)
        if finish <= decision.deadline else 0.0
        for field, decision in enumerate(decisions)
    )


def schedule(decisions, worlds, sources, start_times, budget, aggregate):
    """Select a timed read against a no-read option with value zero."""
    best = None
    best_priority = 0.0
    for start, source in product(sorted(start_times), sources):
        if source.cost > budget:
            continue
        read_gains = gains(decisions, worlds, source, start)
        priority = aggregate(read_gains) - source.cost
        if priority > best_priority + 1e-9:
            best = (source, start)
            best_priority = priority
    return best


def worlds_for(shared_dependence):
    flight_prior = ((None, 0.6), (4, 0.4))
    venue_prior = ((None, 0.6), (2, 0.4))
    transfer_prior = ((None, 1.0),) if shared_dependence else flight_prior
    return tuple(
        World(p_flight * p_venue * p_transfer,
              (flight, flight if shared_dependence else transfer, venue))
        for (flight, p_flight), (venue, p_venue), (transfer, p_transfer) in product(
            flight_prior, venue_prior, transfer_prior,
        )
    )


def run(shared_dependence=True, assume_shared=None):
    decisions = (
        Decision("revise itinerary", 6, 3.0, 4.0),
        Decision("rebook transfer", 7, 3.0, 4.0),
        Decision("reserve alternate venue", 5, 5.0, 6.0),
    )
    worlds = worlds_for(shared_dependence)
    assumed_worlds = worlds if assume_shared is None else worlds_for(assume_shared)
    sources = (
        Source("flight status", (0,), 1, 0.2),
        Source("transfer status", (1,), 2, 0.3),
        Source("venue listing", (2,), 1, 0.2),
    )
    evaluation = {}
    for policy, aggregate in (("best_single", max), ("joint", sum)):
        selected = schedule(decisions, assumed_worlds, sources, range(6), 0.3, aggregate)
        if selected is None:
            evaluation[policy] = None
            continue
        source, start = selected
        # Evaluate acquisition with a common downstream decision rule that knows
        # the actual dependencies. Misspecification affects scheduling only.
        net = sum(gains(decisions, worlds, source, start)) - source.cost
        evaluation[policy] = (source.name, start, source.cost, net)
    return evaluation


if __name__ == "__main__":
    print("condition policy source start cost net_gain")
    conditions = {
        "shared": (True, True),
        "independent": (False, False),
        "incorrect_dependency": (False, True),
    }
    for condition, settings in conditions.items():
        for policy, result in run(*settings).items():
            if result is None:
                print(f"{condition} {policy} no-read - 0.00 0.00")
            else:
                source, start, cost, gain = result
                print(f"{condition} {policy} {source!r} {start} {cost:.2f} {gain:.2f}")
