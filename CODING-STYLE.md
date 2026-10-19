# Coding Style & Principles

## Communication
- When writing for humans (comments, commit messages, replies), use minimal words. Pick every word meticulously. Be direct. Less is more.
- Avoid superlatives and praise. Give cold hard truth.

## Code Quality
- Avoid magic numbers and strings. Extract recurring or meaningful values into descriptive constants or enums. Keep self-explanatory one-off values inline. Values from specs (e.g. HTTP 200 OK) always get a constant.
- Reduce indentation. Avoid Arrow Anti-Pattern. Use early return and continue.
- Keep function names under 30 characters.
- Use enums instead of booleans for function parameters.
- Always use `{}` braces, even on one-line `if` statements.

## Code Readability
- Let the reader breathe. Add empty lines between logical blocks.
- Add small, focused comments explaining *what* and *why*. Use examples when possible.
- Use ASCII drawings to explain complex systems.

## Architecture
- Keep all fields and functions private unless external access is strictly required.
- Program to levels of abstraction. Encapsulate low-level mechanics in dedicated driver/abstraction layers. Expose clean, high-level APIs.
- Adhere to layered boundaries: each layer only communicates with its immediate neighbor below. Never punch holes through layers.

## Discipline
- Don't touch code blocks unrelated to the current feature. Minimize changed lines.
- Prompt for approval before changing any access modifier from private to internal/public.

## Commit Messages
1. Separate subject from body with a blank line
2. Limit subject to 50 characters (72 hard limit)
3. Capitalize the first letter of the subject
4. No period at end of subject
5. Use imperative mood ("Fix bug", not "Fixed bug"). Test: "If applied, this commit will [subject]"
6. Wrap body at 72 characters
7. Body explains what and why, not how

## Bug Fixes
When fixing a bug: write the test first, observe it fail, write the fix, observe the test pass.
