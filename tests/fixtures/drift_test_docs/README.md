# Fixture Repo

This fixture is used by the drift checker's test suite to exercise the
drift checker against a small, controlled mini-repo.

## Valid references

- A working link: [main module](src/main.py)
- Inline code path: `src/main.py`
- Another valid: [helper](src/helper.py)

## Tree

```
.
|-- src/main.py
|-- src/helper.py
```

## Counts

This project has 2 modules and the badge below claims tests-50.

![tests](https://img.shields.io/badge/tests-50-success)

## Allowed exceptions

<!-- drift-ignore -->
This block references `src/does_not_exist.py` but should be ignored.
[also ignored](src/another_missing.py)
<!-- /drift-ignore -->

```sh-example
# Example shell snippet; paths inside are illustrative.
python src/imaginary_example.py
```
