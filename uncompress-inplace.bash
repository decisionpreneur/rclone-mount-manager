#!/usr/bin/env bash
# Replace rclone-compress file pairs with their original files, recursively.
# Usage: uncompress-inplace.bash PATH
# PATH is a filesystem file or directory, including an existing mounted drive.
# Python 3 and rclone are required. RCLONE_BIN selects rclone as in rcmm.bash.
# Each compressed file is fully decoded in RAM before its original is changed.
# The existing data file is renamed, rewritten in place, then its metadata is
# removed. An interrupted rewrite can leave an incomplete file at its original
# name; exclusive access to the selected tree is required during conversion.

set -eu

if (( $# != 1 )); then
	printf 'Usage: %s PATH\n' "${0##*/}" >&2
	exit 2
fi

if [[ ${1} == --help || ${1} == -h ]]; then
	printf '%s\n' 'Usage: uncompress-inplace.bash PATH' \
		'Recursively replace rclone-compress pairs in a filesystem path.' \
		'PATH may also select one original name, data file, or metadata file.' \
		'Existing mounts are used as filesystem paths; remote:path is not a filesystem path.' \
		'One complete decompressed file is buffered in RAM; data is then rewritten in place.' \
		'Exclusive access is required. Interruption during rewriting can leave an incomplete file.'
	exit 0
fi

rclone_bin=${RCLONE_BIN:-}
if [[ -z ${rclone_bin} ]]; then
	rclone_bin=$(command -v rclone || command -v rclone.exe)
fi
python_bin=$(command -v python3 || command -v python)
target=${1}
case $(uname -s) in
	MSYS*|MINGW*|CYGWIN*)
		to_native_path() {
			local drive suffix
			case ${1} in
				/drives/[a-zA-Z]|/drives/[a-zA-Z]/*)
					drive=${1#/drives/}
					suffix=${drive:1}
					printf '%s\n' "${drive:0:1}:${suffix:-/}"
					;;
				*) cygpath -am -- "$(realpath -m -- "${1}")" ;;
			esac
		}
		target=$(to_native_path "${target}")
		rclone_bin=$(to_native_path "${rclone_bin}")
		unset PYTHONHOME PYTHONPATH
		export MSYS2_ARG_CONV_EXCL='*' MSYS_NO_PATHCONV=1
		;;
esac

exec "${python_bin}" -B - "${rclone_bin}" "${target}" <<'PY'
import base64
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import subprocess
import sys


RCLONE, ARGUMENT = sys.argv[1:]
ENCODED = re.compile(r"^(.+)\.([A-Za-z0-9_-]{11})\.(gz|zst)$")
ENV = {k: v for k, v in os.environ.items() if not k.startswith("RCLONE_")}
ENV.update({"MSYS2_ARG_CONV_EXCL": "*", "MSYS_NO_PATHCONV": "1"})
CREATION_FLAGS = (
    subprocess.CREATE_NO_WINDOW | subprocess.IDLE_PRIORITY_CLASS
    if os.name == "nt" else 0
)


def rename_function():
    """Select an atomic, non-replacing rename; never copy between paths."""
    if os.name == "nt":
        return os.rename
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int

        def rename_once(source, destination):
            if rename(-100, os.fsencode(source), -100,
                      os.fsencode(destination), 1):
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code), str(destination))

        return rename_once
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int

        def rename_once(source, destination):
            if rename(os.fsencode(source), os.fsencode(destination), 4):
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code), str(destination))

        return rename_once
    raise OSError("An atomic non-replacing rename is required on this platform")


def decode(directory, original, mode, size):
    """Finish reading/decompressing successfully before permitting a write."""
    data = bytearray(size)
    view = memoryview(data)
    command = [
        RCLONE, "cat", ":compress:" + original,
        "--compress-remote", ":local,encoding=None:" + str(directory),
        "--compress-mode", {2: "gzip", 4: "zstd"}[mode],
        "--config", os.devnull, "--cache-dir", os.devnull,
        "--temp-dir", os.devnull, "--buffer-size", "8Mi",
        "--log-level", "ERROR", "--retries", "1",
    ]
    with subprocess.Popen(command, stdout=subprocess.PIPE, env=ENV,
                          creationflags=CREATION_FLAGS) as process:
        try:
            offset = 0
            while offset < size:
                read = process.stdout.readinto(view[offset:offset + 8 * 1024 * 1024])
                if not read:
                    raise OSError("The decompressed stream ended before its declared size")
                offset += read
            if process.stdout.read(1):
                raise OSError("The decompressed stream exceeds its declared size")
            status = process.wait()
            if status:
                raise OSError("rclone could not decode the file (exit %d)" % status)
        except BaseException:
            if process.poll() is None:
                process.terminate()
            process.wait()
            raise
    return data


def pair(directory, metadata_name, names):
    with (directory / metadata_name).open("r", encoding="utf-8") as source:
        metadata = json.load(source)
    if (not isinstance(metadata, dict)
            or not {"Mode", "Size", "MD5", "MimeType"}.issubset(metadata)):
        return None
    mode, size = metadata.get("Mode"), metadata.get("Size")
    if (type(mode) is not int or mode not in (0, 2, 4)
            or type(size) is not int or not 0 <= size < 2 ** 63
            or not isinstance(metadata.get("MimeType"), str)
            or not re.fullmatch(r"[a-fA-F0-9]{32}", str(metadata.get("MD5", "")))):
        raise ValueError("The paired metadata is not a supported rclone-compress object")
    original = metadata_name[:-5]
    if not original or original in (".", ".."):
        raise ValueError("The original filename is empty or invalid")
    if mode == 0:
        data_name = original + ".bin"
    else:
        encoded_size = base64.urlsafe_b64encode(size.to_bytes(8, "little")).decode().rstrip("=")
        data_name = original + "." + encoded_size + {2: ".gz", 4: ".zst"}[mode]
    if data_name not in names:
        raise FileNotFoundError("The metadata's exact data file is absent: " + data_name)
    return original, data_name, mode, size


def convert(directory, metadata_name, item, rename_once):
    original, data_name, mode, size = item
    source = directory / data_name
    destination = directory / original
    metadata_path = directory / metadata_name
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, "Original filename already exists", str(destination))
    stamp = source.stat(follow_symlinks=False)
    data = decode(directory, original, mode, size) if mode else None
    # A collision at the rename cannot overwrite another file, even if it was
    # created while decoding. Rename retains the sole stored data object.
    rename_once(source, destination)
    try:
        if data is not None:
            with destination.open("r+b", buffering=0) as output:
                view = memoryview(data)
                offset = 0
                while offset < len(view):
                    written = output.write(view[offset:offset + 8 * 1024 * 1024])
                    if not written:
                        raise OSError("The in-place write made no progress")
                    offset += written
                output.truncate(len(view))
            os.utime(destination, ns=(stamp.st_atime_ns, stamp.st_mtime_ns),
                     follow_symlinks=False)
        metadata_path.unlink()
    except BaseException:
        print("INCOMPLETE at %s; metadata retained at %s" %
              (destination, metadata_path), file=sys.stderr, flush=True)
        raise
    print(str(destination), flush=True)


def process_directory(directory, selected, rename_once):
    names, directories, encoded_metadata = set(), [], set()
    payloads = {}
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.is_symlink():
                continue
            if (os.name == "nt" and
                    getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0)
                    == 0xA0000003):
                continue
            if entry.is_dir(follow_symlinks=False):
                directories.append(directory / entry.name)
            elif entry.is_file(follow_symlinks=False):
                names.add(entry.name)
    for name in names:
        match = ENCODED.fullmatch(name)
        if match:
            metadata_name = match[1] + ".json"
            encoded_metadata.add(metadata_name)
            payloads.setdefault(metadata_name, set()).add(name)
        elif name.endswith(".bin") and name[:-4] + ".json" in names:
            metadata_name = name[:-4] + ".json"
            encoded_metadata.add(metadata_name)
            payloads.setdefault(metadata_name, set()).add(name)
    candidates = encoded_metadata | {name for name in names if name.endswith(".json")}
    failures = 0
    recognised = []
    for metadata_name in candidates:
        original = metadata_name[:-5]
        selected_encoded = ENCODED.fullmatch(selected) if selected is not None else None
        if selected is not None and not (
                selected in (metadata_name, original, original + ".bin")
                or (selected_encoded and selected_encoded[1] == original)):
            continue
        try:
            item = pair(directory, metadata_name, names)
            if item is None:
                if metadata_name in encoded_metadata:
                    raise ValueError("The paired metadata is not an rclone-compress object")
                continue
            if payloads.get(metadata_name) != {item[1]}:
                raise ValueError("Multiple data files claim the same original filename")
            if selected is None or selected in (metadata_name, item[0], item[1]):
                recognised.append((metadata_name, item))
        except (json.JSONDecodeError, UnicodeError) as error:
            if metadata_name in encoded_metadata:
                print("ERROR %s: %s" % (directory / metadata_name, error),
                      file=sys.stderr, flush=True)
                failures += 1
        except (OSError, ValueError) as error:
            print("ERROR %s: %s" % (directory / metadata_name, error),
                  file=sys.stderr, flush=True)
            failures += 1
    if selected is not None and len(recognised) != 1:
        raise ValueError("The selected path must identify exactly one rclone-compress pair")
    # A filename can itself end in .json or .bin. Shorter originals release
    # their metadata/data names before longer originals need those names.
    recognised.sort(key=lambda row: len(row[1][0]))
    for metadata_name, item in recognised:
        try:
            convert(directory, metadata_name, item, rename_once)
        except (OSError, ValueError, MemoryError) as error:
            print("ERROR %s: %s" % (directory / item[0], error),
                  file=sys.stderr, flush=True)
            failures += 1
    return directories, failures


def main():
    rename_once = rename_function()
    target = Path(ARGUMENT).absolute()
    directory_target = target.is_dir()
    pending = [target if directory_target else target.parent]
    selected = None if directory_target else target.name
    failures = 0
    while pending:
        directory = pending.pop()
        try:
            children, errors = process_directory(directory, selected, rename_once)
            failures += errors
            if directory_target:
                pending.extend(children)
        except (OSError, ValueError, MemoryError) as error:
            print("ERROR %s: %s" % (directory, error), file=sys.stderr, flush=True)
            failures += 1
    return 1 if failures else 0


try:
    sys.exit(main())
except KeyboardInterrupt:
    sys.exit(130)
except (OSError, ValueError, AttributeError) as error:
    print("ERROR: " + str(error), file=sys.stderr, flush=True)
    sys.exit(1)
PY
