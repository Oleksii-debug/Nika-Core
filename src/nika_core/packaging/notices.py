from __future__ import annotations

import io
import os
import re
import stat
import sys
import tempfile
import unicodedata
from importlib import metadata
from pathlib import Path

RUNTIME_DISTRIBUTIONS = (
    "annotated-types",
    "bottle",
    "cffi",
    "clr-loader",
    "packaging",
    "platformdirs",
    "proxy-tools",
    "pycparser",
    "pydantic",
    "pydantic-core",
    "pydantic-settings",
    "pygments",
    "python-dotenv",
    "pythonnet",
    "pywebview",
    "rich",
    "setuptools",
    "tomli",
    "typing-extensions",
    "typing-inspection",
)

_SECTION_RE = re.compile(r"^===== (?P<title>.+?) =====$")
_MAX_NOTICES_BYTES = 16 * 1024 * 1024
_MAX_DISTRIBUTION_PATH_BYTES = 4096
_MAX_SECTION_IDENTITY_BYTES = 4096
_MAX_NOTICE_SECTIONS = 256
_NOTICE_PREAMBLE = "Nika Core third-party notices"


def _bounded_metadata_value(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        # Runtime metadata corruption is a release-evidence failure, not caller misuse.
        raise RuntimeError(  # noqa: TRY004
            f"Runtime distribution {field} metadata is invalid"
        )
    text = value.strip()
    if not text:
        return None
    if len(text) > _MAX_NOTICES_BYTES:
        raise RuntimeError(
            f"Runtime distribution {field} metadata exceeds the release size limit"
        )
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RuntimeError(f"Runtime distribution {field} metadata is invalid") from exc
    if len(encoded) > _MAX_NOTICES_BYTES:
        raise RuntimeError(
            f"Runtime distribution {field} metadata exceeds the release size limit"
        )
    return text


def _section_identity(value: object, *, field: str) -> str:
    text = _bounded_metadata_value(value, field=field)
    if text is None:
        raise RuntimeError(f"Runtime distribution {field} identity is invalid")
    if len(text) > _MAX_SECTION_IDENTITY_BYTES:
        raise RuntimeError(f"Runtime distribution {field} identity is invalid")
    encoded = text.encode("utf-8")
    if (
        len(encoded) > _MAX_SECTION_IDENTITY_BYTES
        or any(
            unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
            for char in text
        )
    ):
        raise RuntimeError(f"Runtime distribution {field} identity is invalid")
    return text


def _python_license() -> str:
    license_file = Path(sys.base_prefix) / "LICENSE.txt"
    if not license_file.exists():
        raise RuntimeError(f"Python runtime license not found: {license_file}")
    text = _read_notices(license_file)
    if text is None or not text.strip():
        raise RuntimeError("Python runtime license evidence is invalid")
    return text.strip()


def _metadata_license(dist: metadata.Distribution) -> str | None:
    expression = _bounded_metadata_value(
        dist.metadata.get("License-Expression"),
        field="license",
    )
    if expression:
        if any(_SECTION_RE.fullmatch(line.strip()) for line in expression.splitlines()):
            raise RuntimeError("Runtime distribution license metadata is ambiguous")
        return expression

    license_value = _bounded_metadata_value(
        dist.metadata.get("License"),
        field="license",
    )
    if license_value and license_value.upper() != "UNKNOWN":
        if any(_SECTION_RE.fullmatch(line.strip()) for line in license_value.splitlines()):
            raise RuntimeError("Runtime distribution license metadata is ambiguous")
        return license_value

    classifiers: list[str] = []
    classifier_bytes = 0
    for raw_value in dist.metadata.get_all("Classifier", []) or ():
        value = _bounded_metadata_value(raw_value, field="classifier")
        if value is None or not value.startswith("License ::"):
            continue
        classifier_bytes += len(value.encode("utf-8"))
        if classifier_bytes > _MAX_NOTICES_BYTES:
            raise RuntimeError(
                "Runtime distribution classifier metadata exceeds the release size limit"
            )
        classifiers.append(value)
    return "; ".join(classifiers) or None


def _canonical_distribution_file(item: object) -> str:
    relative = str(item).replace("\\", "/")
    if len(relative) > _MAX_DISTRIBUTION_PATH_BYTES:
        raise RuntimeError("Runtime distribution license path identity is invalid")
    try:
        encoded = relative.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            "Runtime distribution license path identity is invalid"
        ) from exc
    if (
        not relative
        or relative != relative.strip()
        or len(encoded) > _MAX_DISTRIBUTION_PATH_BYTES
        or relative.startswith("/")
        or (len(relative) >= 2 and relative[0].isalpha() and relative[1] == ":")
        or any(
            unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
            for char in relative
        )
    ):
        raise RuntimeError("Runtime distribution license path identity is invalid")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError("Runtime distribution license path identity is invalid")
    return relative


def _located_distribution_file(
    dist: metadata.Distribution,
    relative_path: str,
) -> Path:
    try:
        root = Path(dist.locate_file("")).resolve(strict=True)
        target = Path(dist.locate_file(relative_path))
        parent = target.parent.resolve(strict=True)
        common = os.path.commonpath((os.fspath(root), os.fspath(parent)))
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            "Runtime distribution license path containment is invalid"
        ) from exc
    if (
        not root.is_dir()
        or os.path.normcase(common) != os.path.normcase(os.fspath(root))
    ):
        raise RuntimeError("Runtime distribution license path containment is invalid")
    return target


def _license_texts(dist: metadata.Distribution) -> tuple[tuple[str, str], ...]:
    collected: list[tuple[str, str]] = []
    evidence_bytes = 0
    for item in dist.files or ():
        relative_path = str(item).replace("\\", "/")
        leaf = relative_path.rsplit("/", 1)[-1].casefold()
        if not any(marker in leaf for marker in ("license", "licence", "copying", "notice")):
            continue
        relative_path = _canonical_distribution_file(relative_path)
        try:
            path = _located_distribution_file(dist, relative_path)
            text = _read_notices(path)
            if text is None:
                raise RuntimeError("Runtime distribution license evidence is invalid")
            stripped = text.strip()
            if stripped:
                evidence_bytes += len(relative_path.encode("utf-8"))
                evidence_bytes += len(stripped.encode("utf-8"))
                if evidence_bytes > _MAX_NOTICES_BYTES:
                    raise RuntimeError(
                        "Runtime distribution license evidence exceeds the release size limit"
                    )
                collected.append((relative_path, stripped))
        except OSError as exc:
            raise RuntimeError("Runtime distribution license evidence is unreadable") from exc
    return tuple(sorted(collected))


def _distribution_section(
    distribution_name: str,
    dist: metadata.Distribution,
) -> tuple[str, str]:
    package_name = _section_identity(
        dist.metadata.get("Name") or distribution_name,
        field="name",
    )
    version = _section_identity(str(dist.version), field="version")
    declared_license = _metadata_license(dist)
    license_texts = _license_texts(dist)
    if not declared_license and not license_texts:
        raise RuntimeError(
            f"No license evidence found for runtime distribution: {distribution_name}"
        )

    body: list[str] = []
    body_bytes = 0

    def append_body(value: str) -> None:
        nonlocal body_bytes
        encoded_length = len(value.encode("utf-8"))
        body_bytes += encoded_length + (1 if body else 0)
        if body_bytes > _MAX_NOTICES_BYTES:
            raise RuntimeError(
                "Runtime distribution license evidence exceeds the release size limit"
            )
        body.append(value)

    if declared_license:
        append_body(f"Declared license: {declared_license}")
    for relative_path, text in license_texts:
        if body:
            append_body("")
        append_body(f"--- {relative_path} ---")
        append_body(text)
    return f"{package_name} {version}", "\n".join(body).strip()


def build_third_party_notices(bundle_dir: Path) -> Path:
    sections: list[str] = []
    payload_bytes = 0

    def append_section(value: str) -> None:
        nonlocal payload_bytes
        encoded_length = len(value.encode("utf-8"))
        payload_bytes += encoded_length + (1 if sections else 0)
        if payload_bytes > _MAX_NOTICES_BYTES:
            raise RuntimeError("Generated third-party notices exceed the release size limit")
        sections.append(value)

    append_section("Nika Core third-party notices")
    append_section("")
    append_section("===== Python runtime =====")
    append_section(_python_license())
    for distribution_name in RUNTIME_DISTRIBUTIONS:
        try:
            dist = metadata.distribution(distribution_name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                f"Required runtime distribution is missing: {distribution_name}"
            ) from exc
        title, body = _distribution_section(distribution_name, dist)
        append_section("")
        append_section(f"===== {title} =====")
        append_section(body)
    payload = ("\n".join(sections).rstrip() + "\n").encode("utf-8")
    if len(payload) > _MAX_NOTICES_BYTES:
        raise RuntimeError("Generated third-party notices exceed the release size limit")
    target = bundle_dir / "THIRD_PARTY_NOTICES.txt"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".THIRD_PARTY_NOTICES-",
            suffix=".tmp",
            dir=bundle_dir,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def _sections(text: str) -> tuple[dict[str, str], tuple[str, ...]]:
    """Parse the canonical notices grammar without splitlines() amplification."""
    source = io.StringIO(text)
    if source.readline().rstrip("\r\n") != _NOTICE_PREAMBLE:
        raise ValueError("invalid third-party notices preamble")
    if source.readline().rstrip("\r\n") != "":
        raise ValueError("invalid third-party notices preamble separator")

    parsed: dict[str, str] = {}
    duplicates: list[str] = []
    title: str | None = None
    body = io.StringIO()
    section_count = 0

    def commit() -> None:
        nonlocal body
        if title is None:
            return
        value = body.getvalue().strip()
        if title in parsed:
            duplicates.append(title)
        else:
            parsed[title] = value
        body = io.StringIO()

    for raw_line in source:
        line = raw_line.rstrip("\r\n")
        match = _SECTION_RE.fullmatch(line)
        if match:
            commit()
            section_count += 1
            if section_count > _MAX_NOTICE_SECTIONS:
                raise ValueError("too many third-party notice sections")
            title = match.group("title").strip()
            if not title:
                raise ValueError("empty third-party notice section")
            continue
        if title is None:
            raise ValueError("content before first third-party notice section")
        if body.tell():
            body.write("\n")
        body.write(line)
    commit()
    if section_count == 0:
        raise ValueError("third-party notices contain no sections")
    return parsed, tuple(duplicates)


def _windows_verbatim_path(target: Path) -> str:
    absolute = os.path.abspath(os.fspath(target))
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _open_notices_descriptor(target: Path) -> int:
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        generic_read = 0x80000000
        share_read_delete = 0x00000001 | 0x00000004
        open_existing = 3
        file_attribute_normal = 0x00000080
        file_flag_open_reparse_point = 0x00200000
        handle = create_file(
            _windows_verbatim_path(target),
            generic_read,
            share_read_delete,
            None,
            open_existing,
            file_attribute_normal | file_flag_open_reparse_point,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            error = ctypes.get_last_error()
            raise OSError(error, "unable to open third-party notice evidence", str(target))
        try:
            return msvcrt.open_osfhandle(
                handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except Exception:
            close_handle(handle)
            raise

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    return os.open(target, flags)


def _snapshot_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_notices(target: Path) -> str | None:
    """Admit only bounded, regular, stable UTF-8 license evidence."""
    descriptor = -1
    try:
        before = target.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_NOTICES_BYTES:
            return None
        descriptor = _open_notices_descriptor(target)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _snapshot_identity(opened) != _snapshot_identity(before)
        ):
            return None
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            data = source.read(_MAX_NOTICES_BYTES + 1)
            after_first_read = os.fstat(source.fileno())
            if (
                len(data) > _MAX_NOTICES_BYTES
                or len(data) != after_first_read.st_size
                or _snapshot_identity(after_first_read) != _snapshot_identity(opened)
            ):
                return None
            source.seek(0)
            confirmed = source.read(_MAX_NOTICES_BYTES + 1)
            after_second_read = os.fstat(source.fileno())
        current = target.lstat()
        if (
            data != confirmed
            or len(confirmed) > _MAX_NOTICES_BYTES
            or len(confirmed) != after_second_read.st_size
            or _snapshot_identity(after_second_read) != _snapshot_identity(opened)
            or not stat.S_ISREG(current.st_mode)
            or _snapshot_identity(current) != _snapshot_identity(opened)
        ):
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def verify_third_party_notices(bundle_dir: Path) -> tuple[str, ...]:
    target = bundle_dir / "THIRD_PARTY_NOTICES.txt"
    try:
        target.lstat()
    except FileNotFoundError:
        return ("missing:THIRD_PARTY_NOTICES.txt",)
    except OSError:
        return ("notices:unreadable",)

    text = _read_notices(target)
    if text is None:
        return ("notices:unreadable",)
    structural_error = False
    try:
        sections, duplicates = _sections(text)
    except ValueError:
        sections = {}
        duplicates = ()
        structural_error = True

    findings: list[str] = []
    expected_titles = {"Python runtime"}
    if "Python runtime" in duplicates:
        findings.extend(("notices:pythonruntime", "notices:pythonruntime:duplicate"))
    python_body = sections.get("Python runtime")
    try:
        expected_python_body = _python_license()
    except RuntimeError:
        findings.extend(("notices:pythonruntime", "notices:pythonruntime:metadata"))
    else:
        if python_body != expected_python_body:
            findings.append("notices:pythonruntime")

    for distribution_name in RUNTIME_DISTRIBUTIONS:
        base_finding = f"notices:{distribution_name}"
        try:
            dist = metadata.distribution(distribution_name)
            title, expected_body = _distribution_section(distribution_name, dist)
            expected_titles.add(title)
        except (metadata.PackageNotFoundError, RuntimeError):
            findings.extend((base_finding, f"{base_finding}:metadata"))
            continue
        if title in duplicates:
            findings.extend((base_finding, f"{base_finding}:duplicate"))
            continue
        if sections.get(title) != expected_body:
            findings.append(base_finding)

    if set(sections).difference(expected_titles):
        findings.append("notices:unexpected-section")
    if structural_error:
        findings.append("notices:structure")
    return tuple(dict.fromkeys(findings))
