# Testing & Quality Assurance

## 1. Test-Driven Development (TDD)
When addressing bugs or adding API features:
1. **Isolate with a Unit Test:** Write a test reproducing the issue or verifying the new endpoint response parser.
2. **Mock Network Boundaries:** Do not hit live upstream `railway.gov.gr` endpoints in automated CI tests. Use mock responses, fixtures, or recorded SSE stream samples.
3. **Verify Failure (Red):** Ensure the test fails prior to the implementation change.
4. **Implement Minimal Code (Green):** Apply the fix or feature.
5. **Regression Verification:** Run the full test suite to confirm all tests pass cleanly.

## 2. Test Suite Structure
- Tests reside in `tests/`:
  - `tests/test_client.py`: Verifies REST API calls, query parameter construction, and status handling.
  - `tests/test_stream.py`: Tests SSE line buffer parsing, event filtering (`trainPositionsUx`), and chunk fragmentation.
  - `tests/test_models.py`: Validates `TrainPosition.from_dict` and field conversions under complete and incomplete payloads.

## 3. Running Tests
Run tests using Python's standard `unittest` or `pytest`:
```bash
python3 -m unittest discover tests
# or with pytest:
pytest tests/ -v
```
