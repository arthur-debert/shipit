def average(numbers):
    # BUG: no guard for an empty list -> ZeroDivisionError
    return sum(numbers) / len(numbers)
