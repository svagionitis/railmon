# Python Language Rules & Modern Idioms

## 1. Resource Management & Connection Lifecycle
- **Context Managers (`with`):** Always manage network sockets, HTTP response bodies, and file descriptors using `with` blocks to guarantee timely resource release.
- **Generator Pipelines for Streams:** Stream consumers (such as Server-Sent Events from `/api/train-stream`) must be implemented as generators yielding typed domain objects, enabling memory-efficient lazy processing.
- **Graceful Connection Teardown:** Catch network interruptions (`URLError`, `ConnectionError`, `TimeoutError`), log informative warnings, and implement exponential backoff rather than terminating the process abruptly.

## 2. Type Annotations & Data Modeling
- **Type Hints:** Use PEP 484 / Python 3.10+ type hints across all function signatures, return types, and dataclass fields (`Optional[str]`, `List[TrainPosition]`, `Dict[str, Any]`).
- **Dataclasses:** Prefer `@dataclass` for domain entities over plain dictionaries or arbitrary classes.
- **Safe Parsing (`from_dict`):** Never assume remote JSON fields exist or match types. Validate types explicitly, provide sensible defaults, and handle `None`/null values defensively.

## 3. Code Quality & Idiomatic Patterns
- **Early Exits & Guard Clauses:** Avoid deep nested `if` statements. Check for error conditions or empty values early and return/continue immediately.
- **Descriptive Constants:** Extract magic endpoint paths (e.g., `/api/train-stream`, `/api/public/schedules/upcoming`), default timeout limits, and headers into module-level constants.
- **No Bare `except:`:** Always catch specific exceptions (`json.JSONDecodeError`, `urllib.error.HTTPError`, `KeyError`). Never use a bare `except:` clause.
- **Standard Library First:** Keep external dependencies minimal. Standard library modules (`urllib.request`, `json`, `dataclasses`, `ssl`, `time`, `typing`) should be preferred for core functionality, with optional enhancements for `requests` or `aiohttp`.
