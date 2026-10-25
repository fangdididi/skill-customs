"""Print action-like lines from one notes file given on the command line."""
import re
import sys

PATTERN = re.compile(r"^\s*(TODO|ACTION|@\w+)\b[:\s-]*(.*)$", re.IGNORECASE)


def main(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = PATTERN.match(line)
            if m:
                print(f"{m.group(1)}\t{m.group(2).strip()}")


if __name__ == "__main__":
    main(sys.argv[1])
