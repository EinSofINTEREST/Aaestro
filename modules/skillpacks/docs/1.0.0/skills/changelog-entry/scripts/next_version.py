"""Print the next semantic version: next_version.py <current> <major|minor|patch>."""

import sys

BUMPS = ("major", "minor", "patch")


def next_version(current: str, bump: str) -> str:
    parts = current.split(".")
    if len(parts) != len(BUMPS) or not all(part.isdigit() for part in parts):
        raise ValueError(f"not a semantic version: {current}")
    if bump not in BUMPS:
        raise ValueError(f"bump must be one of {', '.join(BUMPS)}: {bump}")
    numbers = [int(part) for part in parts]
    index = BUMPS.index(bump)
    numbers[index] += 1
    numbers[index + 1 :] = [0] * (len(BUMPS) - index - 1)
    return ".".join(str(number) for number in numbers)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: next_version.py <current> <major|minor|patch>")
    try:
        print(next_version(sys.argv[1], sys.argv[2]))
    except ValueError as err:
        sys.exit(str(err))
