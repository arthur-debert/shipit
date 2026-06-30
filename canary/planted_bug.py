"""Throwaway canary module for TRE05-WS04b funnel validation (planted bug)."""


def average(numbers):
    # PLANTED BUG: divides by len(numbers) without guarding the empty case, so
    # average([]) raises ZeroDivisionError instead of returning 0 or raising a
    # clear ValueError. A reviewer should flag this.
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)


def first_even(numbers):
    # PLANTED BUG: returns None implicitly when no even number is found, but the
    # caller below treats the result as always an int, so a missing even silently
    # becomes a TypeError downstream rather than a handled case.
    for n in numbers:
        if n % 2 == 0:
            return n


def describe(numbers):
    even = first_even(numbers)
    return "first even is " + str(even + 1)
