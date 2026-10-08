"""Local private storage with descriptor-based, no-symlink file operations."""
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files import File
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible

NAME_PATTERN = re.compile(r"administrative_expenses/[1-9][0-9]*/[0-9a-f]{32}\.(pdf|jpg|jpeg|png)\Z")


def is_private_media_path(path):
    """Reject lexical paths and resolved aliases into the entire private tree."""
    root = Path(settings.MEDIA_ROOT).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        if "administrative_expenses" in candidate.relative_to(root).parts:
            return True
    except ValueError:
        pass
    try:
        candidate.resolve().relative_to((root / "administrative_expenses").resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


@deconstructible
class PrivateExpenseStorage(FileSystemStorage):
    def url(self, name):
        raise ValueError("Private attachments have no public media URL.")

    def path(self, name):
        if not NAME_PATTERN.fullmatch(name):
            raise SuspiciousFileOperation("Invalid private attachment path.")
        return str(Path(settings.MEDIA_ROOT) / name)

    @contextmanager
    def directory(self, name, create=False):
        self.path(name)
        # Production is Linux. Fail closed if descriptor/no-follow operations are unavailable.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(Path(settings.MEDIA_ROOT).resolve(), flags)
        try:
            for part in name.split("/")[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor, name.rsplit("/", 1)[-1]
        finally:
            os.close(descriptor)

    def exists(self, name):
        try:
            with self.directory(name) as (directory, basename):
                os.stat(basename, dir_fd=directory, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def _save(self, name, content):
        with self.directory(name, create=True) as (directory, basename):
            descriptor = os.open(basename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    for chunk in content.chunks():
                        handle.write(chunk)
            except BaseException:
                try:
                    os.unlink(basename, dir_fd=directory)
                except OSError:
                    pass  # The upload journal retains the name for recovery.
                raise
        return name

    def _open(self, name, mode="rb"):
        if mode != "rb":
            raise ValueError("Private attachments can only be opened for reading.")
        with self.directory(name) as (directory, basename):
            descriptor = os.open(basename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise SuspiciousFileOperation("Invalid attachment.")
            return File(os.fdopen(descriptor, "rb"), name=name)

    def delete(self, name):
        try:
            with self.directory(name) as (directory, basename):
                os.unlink(basename, dir_fd=directory)
        except FileNotFoundError:
            pass


private_storage = PrivateExpenseStorage()
