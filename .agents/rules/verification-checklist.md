# Verification Checklist (Run Before Submitting Code)

Before presenting any Python code snippet or submitting a change in `train-tracker-railway-gov-gr`, verify it against this checklist:

1. **Connection Lifecycle & Resource Safety:**
   - Are all network requests and sockets wrapped in context managers (`with`) or safely closed?
   - Is an explicit `timeout` passed to all HTTP requests (`urllib.request.urlopen`, `requests.get`)?
   - Is reconnect backoff enforced for stream listeners?

2. **Defensive Parsing & Type Safety:**
   - Are all function arguments and returns annotated with standard type hints?
   - Does `TrainPosition.from_dict` handle `None`, missing fields, and type conversions (`float`, `int`, `str`) safely?
   - Are coordinate bounds checked (`-90 <= lat <= 90`, `-180 <= lng <= 180`)?

3. **Relative Paths in Documentation:**
   - Do all documentation links in `docs/` and `README.md` use document-relative paths (`./`, `../`) with no `file://` or machine-specific `/home/` URIs?

4. **Testing & Regressions:**
   - Does the change break existing stream or schedule consumers?
   - Are mock fixtures updated to reflect any upstream schema changes?
