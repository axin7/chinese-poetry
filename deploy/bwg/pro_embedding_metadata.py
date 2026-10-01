"""Extract public model pricing and rate limits from SiliconFlow's pricing page."""

import argparse
import datetime
import html.parser
import json
import pathlib
import re

MODELS = ("BAAI/bge-m3", "Pro/BAAI/bge-m3")


class Scripts(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        self.active = tag == "script"

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.scripts.append(data)


def objects(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from objects(nested)


def records(path):
    parser = Scripts()
    parser.feed(path.read_text())
    stream = []
    for script in parser.scripts:
        match = re.fullmatch(r"self\.__next_f\.push\((.*)\)", script, re.DOTALL)
        if match:
            chunk = json.loads(match.group(1))
            if len(chunk) > 1 and isinstance(chunk[1], str):
                stream.append(chunk[1])
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?:^|\n)[0-9a-f]+:", "".join(stream)):
        try:
            value, _ = decoder.raw_decode("".join(stream), match.end())
        except json.JSONDecodeError:
            continue
        yield from objects(value)


def metadata(path):
    found = {}
    for value in records(path):
        name = value.get("modelName")
        if name not in MODELS or not isinstance(value.get("pricing"), list):
            continue
        found[name] = {key: value.get(key) for key in (
            "modelName", "targetModelName", "contextLen", "tags", "status",
            "priceUnit", "pricing", "modelInfo",
        )}
    if set(found) != set(MODELS):
        raise ValueError("Pricing page did not expose both complete model records")
    return {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source": "https://siliconflow.cn/pricing", "currency": "CNY", "models": found}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", required=True, type=pathlib.Path)
    parser.add_argument("--report", required=True, type=pathlib.Path)
    args = parser.parse_args()
    result = metadata(args.html)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
