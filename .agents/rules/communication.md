# Human Communication & Code Philosophy

## 1. Human Communication
- **Extreme Conciseness:** When writing something intended for human consumption (code comments, commit messages, plan descriptions, replies), use as few words as possible. Pick every word meticulously to reduce the volume to a strict minimum. Be down to the point. Less is more.
- **No Flattery or Superlatives:** Avoid praise, sycophancy, or superlatives. Give the cold hard truth and objective technical analysis.

## 2. Planning & Changes
- **Implementation Plan First:** For all non-trivial code changes, create an implementation plan first.
- **Root-Cause Bug Analysis:** When fixing a bug, explain in detail what caused it, how it was fixed, and what the architectural implications of the fix are.
- **Minimal Scope & Diffs:**
  - Don't touch blocks of code unrelated to the feature or fix (e.g., do not reformat or comment code you did not modify).
  - Minimize the number of changed lines to keep git history clean and reviewable.

## 3. Code Structure Philosophy
- **Early Exit / Guard Clauses:** Reduce nesting and avoid the Arrow Anti-Pattern. Leverage early `return` and `continue`.
- **Function Names:** Keep function names short, clear, and descriptive: fewer than 30 characters.
- **Enums over Booleans:** Use strongly typed `Enum` instead of boolean parameters in function signatures to make call sites readable and explicit.
- **Readability & Spacing:** Add empty lines between logical blocks of code to let the code breathe.
- **Contextual Comments & Diagrams:** Add small, focused comments explaining *what* a block does and *why*. Use ASCII diagrams to clarify complex protocol frames, coordinate systems, or stream state transitions in code comments. In Markdown documentation, provide both ASCII diagrams and Mermaid diagrams, and ensure all referenced filenames use document-relative paths per `.agents/rules/diagrams-and-visual-documentation.md`.
- **Member Encapsulation:** Prefix internal methods and variables with `_` unless public access is intended.
