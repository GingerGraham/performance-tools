"""File helpers that close what they open, so the suite runs without ResourceWarning."""


def read_text(path, encoding=None):
    with open(path, encoding=encoding) as handle:
        return handle.read()


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def write_text(path, text):
    with open(path, "w") as handle:
        handle.write(text)


def write_bytes(path, data):
    with open(path, "wb") as handle:
        handle.write(data)
