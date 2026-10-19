"""Bounded, read-only access to an explicitly shared tree of source repositories.

This module is deliberately independent of the bot and Codex runtimes.  It never
runs repository programs or Git.  All user-controlled path traversal is relative
only to an open root descriptor and refuses symbolic links at every component.
Returned source is untrusted data; redaction is a further precaution, not a
replacement for keeping credentials out of the shared source tree.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple
from xml.parsers import expat


MAX_FILE_BYTES = 512 * 1024
MAX_SEARCH_BYTES = 24 * 1024 * 1024
MAX_SEARCH_FILES = 2500
MAX_ENTRIES = 30000
MAX_DEPTH = 16
MAX_SECONDS = 3.0
MAX_RESULTS = 100
MAX_REPOSITORIES = 200
MAX_OUTPUT_CHARS = 48000
MAX_LINE_CHARS = 2000

_SOURCE_SUFFIXES = frozenset((
    ".py", ".pyi", ".java", ".kt", ".kts", ".scala", ".go", ".rs",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".cs",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".sql", ".graphql", ".gql", ".proto", ".thrift", ".rb", ".php",
    ".swift", ".dart", ".ex", ".exs", ".clj", ".cljs", ".cljc",
    ".erl", ".hrl", ".hs", ".lua", ".pl", ".r", ".m", ".mm",
    ".sh", ".bash", ".zsh", ".md", ".rst", ".txt", ".html", ".css",
    ".scss", ".less",
))
_EXCLUDED_DIRECTORIES = frozenset((
    "node_modules", "vendor", "vendors", "bower_components", "site-packages",
    "venv", "env", "__pycache__", "target", "build", "dist", "out", "bin",
    "obj", "coverage", "generated", "generated-sources", "logs", "log",
    "tmp", "temp", "cache", "secrets", "credentials", "certs", "certificates",
))
_SENSITIVE_PART = re.compile(
    r"(?:^|[._-])(?:credentials?|secrets?|keystores?|truststores?|passwords?|"
    r"passwd|shadow|tokens?|private[-_]?keys?|id_rsa|id_ecdsa|id_ed25519|"
    r"authorized_keys|known_hosts|dump|backup)(?:[._-]|$)", re.I,
)
_CONFIG_NAME = re.compile(
    r"^(?:config|settings|environment|application|bootstrap|local|production|"
    r"staging|development)(?:[._-]|$)", re.I,
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)(\b(?:[a-z_][a-z0-9_.-]*[_.-]?)?"
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key(?:[_-]?id)?|"
    r"secret[_-]?key|private[_-]?key|authorization)"
    r"[\"']?\s*(?::|=|\()\s*)[^\r\n]+"
)
_AUTH = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_KNOWN_KEY = re.compile(
    r"\b(?:AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|"
    r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{16,}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"
)
_URL_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_+/=-]{40,}(?![A-Za-z0-9_])")
_COMMIT = re.compile(r"[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?\Z")
_REF = re.compile(r"refs/heads/[A-Za-z0-9_./-]+\Z")


def redact(text: str) -> str:
    """Remove common credential formats while preserving source line numbers."""
    text = _PRIVATE_KEY.sub(
        lambda match: "[REDACTED PRIVATE KEY]" + "\n" * match.group(0).count("\n"), text,
    )
    text = _SECRET_ASSIGNMENT.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = _AUTH.sub(lambda match: match.group(1) + " [REDACTED]", text)
    text = _KNOWN_KEY.sub("[REDACTED]", text)
    text = _URL_CREDENTIALS.sub(lambda match: match.group(1) + "[REDACTED]@", text)

    def hide_entropy(match: re.Match) -> str:
        value = match.group(0)
        if not (any(c.isalpha() for c in value) and any(c.isdigit() for c in value)):
            return value
        entropy = -sum((value.count(c) / len(value)) * math.log2(value.count(c) / len(value))
                       for c in set(value))
        return "[REDACTED]" if entropy >= 4.0 else value

    return _LONG_TOKEN.sub(hide_entropy, text)


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("%s must be an integer between %d and %d" % (name, minimum, maximum))
    return value


def _safe_component(value: str) -> bool:
    return (bool(value) and not value.startswith(".") and value not in (".", "..")
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and not any(c in value for c in ("/", "\\", ":"))
            and not _SENSITIVE_PART.search(value))


def _directory_allowed(name: str) -> bool:
    return _safe_component(name) and name.casefold() not in _EXCLUDED_DIRECTORIES


def _file_allowed(name: str) -> bool:
    lower = name.casefold()
    return (_safe_component(name) and not _CONFIG_NAME.search(name)
            and (Path(lower).suffix in _SOURCE_SUFFIXES or name.endswith("Mapper.xml"))
            and not lower.endswith((".min.js", ".min.css", ".map", ".lock")))


def _mapper_source(text: str) -> str:
    """Expose only raw top-level select/sql spans, without evaluating MyBatis.

    Expat checks well-formedness, but never parses the external DTD or resolves
    entities. Only the standard MyBatis declaration is accepted; all internal
    subsets are forbidden. Byte spans retain literal dynamic tags and CDATA.
    Removing other text preserves newlines, including when tags share a line.
    """
    raw = text.encode("utf-8")
    parser = expat.ParserCreate("UTF-8", namespace_separator="}")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    stack: List[str] = []
    spans: List[Tuple[int, int]] = []
    selected: Optional[Tuple[int, Optional[int]]] = None

    def forbidden(*args: Any) -> None:
        raise ValueError("Mapper XML entities and internal DTD subsets are forbidden")

    def doctype(name: str, system: Optional[str], public: Optional[str], internal: bool) -> None:
        if (internal or name != "mapper" or public != "-//mybatis.org//DTD Mapper 3.0//EN"
                or system not in {"https://mybatis.org/dtd/mybatis-3-mapper.dtd",
                                  "http://mybatis.org/dtd/mybatis-3-mapper.dtd"}):
            raise ValueError("only the fixed MyBatis Mapper DOCTYPE is permitted")

    def opening_end(start: int) -> int:
        quote = 0
        for index in range(start, len(raw)):
            byte = raw[index]
            if quote:
                if byte == quote:
                    quote = 0
            elif byte in (ord('"'), ord("'")):
                quote = byte
            elif byte == ord(">"):
                return index + 1
        raise ValueError("invalid Mapper XML opening tag")

    def start(name: str, attributes: Dict[str, str]) -> None:
        nonlocal selected
        if not stack and name != "mapper":
            raise ValueError("only MyBatis mapper roots are available")
        if len(stack) >= 128:
            raise ValueError("Mapper XML nesting exceeds the limit")
        if len(stack) == 1 and name in {"select", "sql"}:
            offset = parser.CurrentByteIndex
            end = opening_end(offset)
            selected = (offset, end if raw[end - 2:end] == b"/>" else None)
        stack.append(name)

    def end(name: str) -> None:
        nonlocal selected
        if len(stack) == 2 and name in {"select", "sql"} and selected is not None:
            beginning, self_closing_end = selected
            closing_end = self_closing_end or (raw.find(b">", parser.CurrentByteIndex) + 1)
            if closing_end <= beginning:
                raise ValueError("invalid Mapper XML closing tag")
            spans.append((beginning, closing_end))
            selected = None
        stack.pop()

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.SkippedEntityHandler = forbidden
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(raw, True)
    except expat.ExpatError:
        raise ValueError("invalid or unsupported Mapper XML") from None

    def hidden_newlines(fragment: bytes) -> bytes:
        # Count breaks before removing text: CR + hidden content + LF are two
        # lines, and must not collapse into a single CRLF after masking.
        return b"\n" * len(re.findall(r"\r\n|[\r\n\x85\u2028\u2029]", fragment.decode("utf-8")))

    fragments: List[bytes] = []
    offset = 0
    for beginning, ending in spans:
        fragments.append(hidden_newlines(raw[offset:beginning]))
        fragments.append(raw[beginning:ending])
        offset = ending
    fragments.append(hidden_newlines(raw[offset:]))
    result = b"".join(fragments).decode("utf-8")
    # splitlines() drops a final empty line unless it has a terminator. The
    # closing mapper line is hidden even when the original file had no final LF.
    return result + ("\n" if text and not text.endswith(("\r", "\n", "\x85", "\u2028", "\u2029")) else "")


class _Budget:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + MAX_SECONDS
        self.entries = 0
        self.files = 0
        self.bytes = 0
        self.truncated = False

    def available(self) -> bool:
        if (time.monotonic() >= self.deadline or self.entries >= MAX_ENTRIES
                or self.files >= MAX_SEARCH_FILES or self.bytes >= MAX_SEARCH_BYTES):
            self.truncated = True
            return False
        return True


class CodeReader:
    """Read source only beneath ``root``; call ``close`` when discarding a reader."""

    def __init__(self, root: Path):
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise RuntimeError("secure source access requires O_NOFOLLOW and O_DIRECTORY")
        self._root_fd = -1
        # Preserve the supplied path components: resolving first would hide links.
        supplied = Path(root).expanduser()
        if not supplied.is_absolute():
            raise ValueError("code root must be an absolute directory")
        if ".." in supplied.parts:
            raise ValueError("code root must not contain parent traversal")
        fd = os.open(os.sep, self._directory_flags())
        try:
            for part in supplied.parts[1:]:
                next_fd = os.open(part, self._directory_flags(), dir_fd=fd)
                os.close(fd)
                fd = next_fd
            self._root_fd = fd
        except OSError as exc:
            os.close(fd)
            raise ValueError("code root must be a real directory without symbolic links") from None
        # Never return this host path in tool output.
        self.root = supplied

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> "CodeReader":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError):
            pass

    def _parts(self, path: str, directory: bool = False) -> List[str]:
        if not isinstance(path, str) or len(path) > 2048:
            raise ValueError("path must be a bounded relative source path")
        parts = path.split("/")
        if not parts or not all(_directory_allowed(p) for p in parts[:-1]):
            raise ValueError("path is outside the shared source policy")
        if not (_directory_allowed(parts[-1]) if directory else _file_allowed(parts[-1])):
            raise ValueError("path is outside the shared source policy")
        if len(parts) > MAX_DEPTH:
            raise ValueError("source path exceeds the depth limit")
        return parts

    def _open_directory(self, parts: List[str]) -> int:
        if self._root_fd < 0:
            raise ValueError("code reader is closed")
        fd = os.dup(self._root_fd)
        try:
            for part in parts:
                next_fd = os.open(part, self._directory_flags(), dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except OSError:
            os.close(fd)
            raise FileNotFoundError("source directory is unavailable or forbidden") from None

    def _read_at(self, directory_fd: int, name: str, maximum: int = MAX_FILE_BYTES) -> bytes:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
                raise ValueError("source file is not an eligible bounded regular file")
            chunks = []
            count = 0
            while count <= maximum:
                chunk = os.read(fd, min(65536, maximum + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
            if count > maximum:
                raise ValueError("source file exceeds the size limit")
            return b"".join(chunks)
        finally:
            os.close(fd)

    @staticmethod
    def _text(data: bytes, name: str = "") -> str:
        if any(byte < 32 and byte not in (9, 10, 13) for byte in data):
            raise ValueError("binary source files are unavailable")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("only UTF-8 source files are available") from None
        return redact(_mapper_source(text) if name.endswith("Mapper.xml") else text)

    def _revision(self, repository: str) -> Optional[str]:
        """Read only HEAD and an optional loose ref; never execute Git or hooks."""
        fd = -1
        try:
            fd = self._open_directory([repository, ".git"])
            head = self._read_at(fd, "HEAD", 1024).decode("ascii").strip()
            if _COMMIT.fullmatch(head):
                return head.lower()
            if not head.startswith("ref: ") or not _REF.fullmatch(head[5:]):
                return None
            ref = head[5:].split("/")
            if any(part in ("", ".", "..") or part.startswith(".") for part in ref):
                return None
            for part in ref[:-1]:
                next_fd = os.open(part, self._directory_flags(), dir_fd=fd)
                os.close(fd)
                fd = next_fd
            value = self._read_at(fd, ref[-1], 128).decode("ascii").strip()
            return value.lower() if _COMMIT.fullmatch(value) else None
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        finally:
            if fd >= 0:
                os.close(fd)

    def list_repositories(self) -> Dict[str, Any]:
        budget = _Budget()
        repositories = []
        fd = self._open_directory([])
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    if not budget.available():
                        break
                    budget.entries += 1
                    if not _directory_allowed(entry.name):
                        continue
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        check = os.open(entry.name, self._directory_flags(), dir_fd=fd)
                        os.close(check)
                    except OSError:
                        continue
                    if len(repositories) >= MAX_REPOSITORIES:
                        budget.truncated = True
                        break
                    repositories.append({"repository": entry.name, "revision": self._revision(entry.name)})
        finally:
            os.close(fd)
        return {"repositories": sorted(repositories, key=lambda item: item["repository"]),
                "truncated": budget.truncated, "source": "working_tree",
                "revision_note": "HEAD only; working tree may contain uncommitted changes."}

    def _walk(self, fd: int, parts: List[str], budget: _Budget) -> Iterator[Tuple[str, bytes]]:
        with os.scandir(fd) as entries:
            for entry in entries:
                if not budget.available():
                    return
                budget.entries += 1
                name = entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not _directory_allowed(name):
                            continue
                        if len(parts) >= MAX_DEPTH - 1:
                            budget.truncated = True
                            continue
                        child = os.open(name, self._directory_flags(), dir_fd=fd)
                        try:
                            yield from self._walk(child, parts + [name], budget)
                        finally:
                            os.close(child)
                    elif _file_allowed(name) and entry.is_file(follow_symlinks=False):
                        budget.files += 1
                        data = self._read_at(fd, name)
                        budget.bytes += len(data)
                        if budget.bytes > MAX_SEARCH_BYTES:
                            budget.truncated = True
                            return
                        yield "/".join(parts + [name]), data
                except (OSError, ValueError):
                    # Concurrent deletions, forbidden links, and unreadable files
                    # must never expose their exception strings or host paths.
                    continue

    def search(self, query: str, repository: str = "", limit: int = 30) -> Dict[str, Any]:
        _integer(limit, "limit", 1, MAX_RESULTS)
        if (not isinstance(query, str) or not 1 <= len(query) <= 256
                or any(ord(c) < 32 or ord(c) == 127 for c in query)):
            raise ValueError("query must be 1 to 256 printable characters")
        if not isinstance(repository, str):
            raise ValueError("repository must be a relative repository name")
        parts = self._parts(repository, directory=True) if repository else []
        if len(parts) > 1:
            raise ValueError("repository must name one immediate child of the shared root")
        fd = self._open_directory(parts)
        budget = _Budget()
        matches = []
        revisions = {}
        output_chars = 0
        walker = self._walk(fd, parts, budget)
        try:
            for path, raw in walker:
                try:
                    content = self._text(raw, path)
                except ValueError:
                    continue
                for number, line in enumerate(content.splitlines(), 1):
                    if not budget.available():
                        break
                    position = line.find(query)
                    if position < 0:
                        continue
                    if len(matches) >= limit or output_chars >= MAX_OUTPUT_CHARS:
                        budget.truncated = True
                        break
                    start = max(0, position - 200)
                    snippet = line[start:start + min(MAX_LINE_CHARS, MAX_OUTPUT_CHARS - output_chars)]
                    output_chars += len(snippet)
                    matches.append({"path": path, "line": number, "content": snippet,
                                    "line_truncated": start > 0 or len(snippet) < len(line)})
                    repo = path.split("/", 1)[0] if "/" in path else ""
                    if repo and repo not in revisions:
                        revisions[repo] = self._revision(repo)
                if budget.truncated:
                    break
        finally:
            walker.close()
            os.close(fd)
        return {"matches": matches, "truncated": budget.truncated,
                "files_examined": budget.files, "revisions": revisions, "source": "working_tree",
                "revision_note": "HEAD only; working tree may contain uncommitted changes."}

    def read(self, path: str, start_line: int = 1, line_count: int = 120) -> Dict[str, Any]:
        _integer(start_line, "start_line", 1, 1000000)
        _integer(line_count, "line_count", 1, 200)
        parts = self._parts(path)
        fd = self._open_directory(parts[:-1])
        try:
            try:
                content = self._text(self._read_at(fd, parts[-1]), parts[-1])
            except OSError:
                raise FileNotFoundError("source file is unavailable or forbidden") from None
        finally:
            os.close(fd)
        lines = content.splitlines()
        selected = []
        chars = 0
        truncated = False
        for number in range(start_line, min(len(lines) + 1, start_line + line_count)):
            original = lines[number - 1]
            line = original[:min(MAX_LINE_CHARS, MAX_OUTPUT_CHARS - chars)]
            selected.append({"line": number, "content": line, "line_truncated": len(line) < len(original)})
            chars += len(line)
            truncated = truncated or len(line) < len(original)
            if chars >= MAX_OUTPUT_CHARS:
                truncated = True
                break
        next_line = start_line + len(selected)
        return {"path": "/".join(parts), "start_line": start_line, "lines": selected,
                "total_lines": len(lines), "next_line": next_line if next_line <= len(lines) else None,
                "truncated": truncated, "revision": self._revision(parts[0]) if len(parts) > 1 else None,
                "source": "working_tree",
                "revision_note": "HEAD only; working tree may contain uncommitted changes."}
