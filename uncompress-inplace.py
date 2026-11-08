#!/usr/bin/env python3
"""Replace rclone-compress pairs in a filesystem path, using Python 3.14+.

Usage: uncompress-inplace.py PATH
A directory is processed recursively; a file selects its own pair.
Compressed input is buffered in RAM; decoded data is streamed in large blocks.
Writes replace the existing payload before renaming it.
The caller must have exclusive write access to the selected files. An interrupted
in-place write can leave an incomplete payload; metadata stays until completion.
"""
import sys
sys.dont_write_bytecode = True

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from compression import zstd
from contextlib import contextmanager
import gzip
import hashlib
import io
import os
from pathlib import Path
from queue import Queue
import re
from threading import Lock
import time
import zlib

CHUNK = 8 * 1024 * 1024
ENCODED = re.compile(r"^(.+)\.([A-Za-z0-9_-]{11})\.(gz|zst)$")
OUTPUT_LOCK = Lock()


class InvalidPayload(ValueError):
    pass


def report(phase, path, amount=None):
    suffix = "" if amount is None else " " + str(amount)
    with OUTPUT_LOCK:
        print(phase + " " + str(path) + suffix, flush=True)


@contextmanager
def descriptor(path, flags):
    handle = os.open(path, flags | getattr(os, "O_BINARY", 0))
    try:
        yield handle
    finally:
        os.close(handle)


def read_bytes(path):
    with descriptor(path, os.O_RDONLY) as handle, io.BytesIO() as buffer:
        while chunk := os.read(handle, CHUNK):
            buffer.write(chunk)
        return buffer.getvalue()


def fingerprint(read, path, phase, size=None):
    digest = hashlib.md5(usedforsecurity=False)
    count = 0
    next_report = time.monotonic() + 10
    while chunk := read(CHUNK):
        end = count + len(chunk)
        if size is not None and end > size:
            raise InvalidPayload("Decoded data exceeds the metadata size")
        digest.update(chunk)
        count = end
        if time.monotonic() >= next_report:
            report(phase, path, count)
            next_report = time.monotonic() + 10
    if size is not None and count != size:
        raise InvalidPayload("Expected %d bytes, read %d" % (size, count))
    return count, digest.hexdigest()


def raw_fingerprint(path):
    with descriptor(path, os.O_RDONLY) as handle:
        return fingerprint(lambda count: os.read(handle, count), path, "VERIFY")


@contextmanager
def decoder(data):
    magic = data[:4]
    with io.BytesIO(data) as source:
        if magic[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=source, mode="rb") as stream:
                yield stream
        elif (magic == b"\x28\xb5\x2f\xfd"
              or 0x184D2A50 <= int.from_bytes(magic, "little") <= 0x184D2A5F):
            with zstd.ZstdFile(source, "rb") as stream:
                yield stream
        else:
            raise InvalidPayload("Payload is neither the original data nor gzip/zstd")


def matches(path, metadata):
    return raw_fingerprint(path) == (metadata["Size"], metadata["MD5"])


def prepare(path, metadata):
    """Accept plaintext or compressed bytes only when they match the source MD5."""
    report("READ", path)
    if path.stat().st_size == metadata["Size"] and matches(path, metadata):
        return None
    data = read_bytes(path)
    with decoder(data) as stream:
        _, digest = fingerprint(stream.read, path, "DECODE", metadata["Size"])
    if digest != metadata["MD5"]:
        raise InvalidPayload("Decoded MD5 differs from the source metadata")
    return data


def redundant(path, metadata):
    """Distinguish a duplicate or an incomplete encoded payload from another file."""
    if path.stat().st_size == metadata["Size"] and matches(path, metadata):
        return True
    encoded = ENCODED.fullmatch(path.name)
    nominal = (int.from_bytes(base64.urlsafe_b64decode(encoded[2] + "="), "little")
               if encoded else None)
    try:
        with decoder(read_bytes(path)) as stream:
            size, digest = fingerprint(stream.read, path, "READ-EXTRA")
    except (InvalidPayload, EOFError, gzip.BadGzipFile, zlib.error, zstd.ZstdError):
        return encoded is not None
    if (size, digest) == (metadata["Size"], metadata["MD5"]):
        return True
    return nominal is not None and size != nominal


def metadata_for(path):
    import json
    obj = json.loads(read_bytes(path))
    fields = {"Mode", "Size", "MD5", "MimeType"}
    if not isinstance(obj, dict) or not fields.issubset(obj):
        return None
    if (type(obj["Mode"]) is not int or obj["Mode"] not in (0, 2, 4)
            or type(obj["Size"]) is not int or not 0 <= obj["Size"] < 2 ** 63
            or not isinstance(obj["MimeType"], str)
            or not isinstance(obj["MD5"], str)
            or re.fullmatch(r"[0-9a-fA-F]{32}", obj["MD5"]) is None):
        raise InvalidPayload("Invalid rclone-compress metadata")
    obj["MD5"] = obj["MD5"].lower()
    return {name: obj[name] for name in fields}


def expected_payload(original, metadata):
    if metadata["Mode"] == 0:
        return original + ".bin"
    size = base64.urlsafe_b64encode(
        metadata["Size"].to_bytes(8, "little")).decode("ascii").rstrip("=")
    return original + "." + size + {2: ".gz", 4: ".zst"}[metadata["Mode"]]


def convert(sidecar, metadata, payload_names):
    original = sidecar.name[:-5]
    if not original or original in (".", ".."):
        raise InvalidPayload("Invalid original filename")
    directory = sidecar.parent
    destination = directory / original
    payloads = [directory / name for name in sorted(payload_names)]
    expected = directory / expected_payload(original, metadata)
    if expected in payloads:
        payloads.remove(expected)
        payloads.insert(0, expected)

    source = None
    data = None
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise InvalidPayload("Original name is occupied by another object")
        # This also resumes a completed rewrite whose sidecar remains.
        data = prepare(destination, metadata)
        source = destination
    else:
        problems = []
        for candidate in payloads:
            try:
                data = prepare(candidate, metadata)
                source = candidate
                break
            except (InvalidPayload, EOFError, gzip.BadGzipFile, zlib.error, zstd.ZstdError) as error:
                problems.append(candidate.name + ": " + str(error))
        if source is None:
            raise InvalidPayload("; ".join(problems) or "The data payload is absent")

    for extra in payloads:
        if extra != source and not redundant(extra, metadata):
            raise InvalidPayload("Distinct data shares the original name: " + str(extra))

    stamp = source.stat()
    if data is not None:
        report("WRITE", source, metadata["Size"])
        # O_TRUNC and write-only sequential IO also work on cache-off mounts.
        # O_CREAT is deliberately absent: a removed source is not recreated.
        with decoder(data) as stream, descriptor(source, os.O_WRONLY | os.O_TRUNC) as handle:
            offset = 0
            next_report = time.monotonic() + 10
            while chunk := stream.read(CHUNK):
                view = memoryview(chunk)
                written = 0
                while written < len(view):
                    count = os.write(handle, view[written:])
                    if not count:
                        raise OSError("The in-place write made no progress")
                    written += count
                offset += written
                if time.monotonic() >= next_report:
                    report("WRITE", source, offset)
                    next_report = time.monotonic() + 10
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        report("VERIFY", source)
        if not matches(source, metadata):
            raise InvalidPayload("Written data differs from the source metadata")
        del data

    if source != destination:
        if os.path.lexists(destination):
            raise FileExistsError("Original name appeared during conversion: " + str(destination))
        source.rename(destination)
    for extra in payloads:
        if extra != source:
            extra.unlink()
    sidecar.unlink()
    report("OK", destination, metadata["Size"])
    return metadata["Size"]


def scan_directory(directory, selected):
    import json
    names, children = set(), []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.is_symlink() or entry.is_junction():
                continue
            if entry.is_dir(follow_symlinks=False):
                children.append(directory / entry.name)
            elif entry.is_file(follow_symlinks=False):
                names.add(entry.name)

    payloads = {}
    for name in names:
        encoded = ENCODED.fullmatch(name)
        if encoded:
            payloads.setdefault(encoded[1] + ".json", set()).add(name)
        elif name.endswith(".bin") and name[:-4] + ".json" in names:
            payloads.setdefault(name[:-4] + ".json", set()).add(name)

    candidates = {name for name in names if name.endswith(".json")} | payloads.keys()
    records = []
    failures = 0
    selected_encoded = ENCODED.fullmatch(selected) if selected else None
    for name in candidates:
        original = name[:-5]
        if selected is not None and not (
                selected in (name, original, original + ".bin")
                or (selected_encoded and selected_encoded[1] == original)):
            continue
        try:
            metadata = metadata_for(directory / name)
            if metadata is None:
                if name in payloads:
                    raise InvalidPayload("Encoded payload has unrelated metadata")
                continue
            records.append((name, metadata, payloads.get(name, set())))
        except (json.JSONDecodeError, UnicodeError) as error:
            if name in payloads:
                report("ERROR", directory / name, str(error))
                failures += 1
        except (OSError, ValueError) as error:
            report("ERROR", directory / name, str(error))
            failures += 1

    if selected is not None and len(records) != 1:
        raise InvalidPayload("The path must identify exactly one rclone-compress object")
    records.sort(key=lambda row: len(row[0]))
    return children, records, failures


def convert_records(directory, records):
    if records:
        report("DIRECTORY", directory)
    failures = 0
    completed = total = 0
    for name, metadata, data_names in records:
        try:
            total += convert(directory / name, metadata, data_names)
            completed += 1
        except (OSError, ValueError, EOFError, MemoryError, zlib.error, zstd.ZstdError) as error:
            report("ERROR", directory / name, str(error))
            failures += 1
    return completed, total, failures


def directory_plans(root, selected):
    """Overlap directory reads; keep all payload rewrites in the calling thread."""
    ready = Queue()
    lock = Lock()
    pending = 1
    scanned = 0
    with ThreadPoolExecutor() as pool:
        def scan(directory):
            nonlocal pending, scanned
            try:
                children, records, failures = scan_directory(directory, selected)
            except Exception as error:
                report("ERROR", directory, str(error))
                children, records, failures = [], [], 1
            with lock:
                if selected is None:
                    pending += len(children)
                    for child in children:
                        pool.submit(scan, child)
                ready.put((directory, records, failures))
                scanned += 1
                if scanned % 100 == 0:
                    report("SCANNED", root, scanned)
                pending -= 1
                if pending == 0:
                    ready.put(None)

        pool.submit(scan, root)
        while (plan := ready.get()) is not None:
            yield plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    target = parser.parse_args().path.expanduser().absolute()
    report("START", target)
    directory_target = target.is_dir()
    selected = None if directory_target else target.name
    root = target if directory_target else target.parent
    completed = total = failures = 0
    for directory, records, scan_errors in directory_plans(root, selected):
        count, size, errors = convert_records(directory, records)
        completed += count
        total += size
        failures += errors + scan_errors
        if records or scan_errors:
            report("TOTAL", target, "%d files; %d bytes; %d errors" %
                   (completed, total, failures))
    report("FINISHED", target, "%d files; %d bytes; %d errors" %
           (completed, total, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
