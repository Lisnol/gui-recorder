from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile

import yaml


def atomic_write_utf8(path: Path, content: str, *, validate_yaml: bool = False) -> None:
    """Atomically replace *path* with UTF-8 text.

    The temporary file lives beside the destination so ``os.replace`` stays on
    the same filesystem. Existing permission bits are retained when possible.
    YAML validation happens before a temporary file is created, leaving the
    current file untouched when the generated content is malformed.
    """
    path = Path(path)

    if validate_yaml:
        try:
            yaml.compose(content)
        except yaml.YAMLError as err:
            raise ValueError(f"Refusing to write invalid YAML to {path.name}.") from err

    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        pass

    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    descriptor_open = True

    try:
        if existing_mode is not None:
            os.fchmod(file_descriptor, existing_mode)

        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor_open = False
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, path)

        # Persist the directory entry as well as the file contents. Some
        # platforms do not allow opening directories; the file replacement is
        # still atomic there, so directory fsync remains best-effort.
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            directory_descriptor = os.open(path.parent, directory_flags)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                try:
                    os.fsync(directory_descriptor)
                except OSError:
                    # The replacement is still atomic on filesystems/platforms
                    # that do not support syncing directory handles.
                    pass
            finally:
                os.close(directory_descriptor)
    finally:
        if descriptor_open:
            os.close(file_descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
