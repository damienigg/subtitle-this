"""Single source of truth for the running version.

`pyproject.toml` carries the same string for the build system, but at
runtime we read from here rather than parsing pyproject (which isn't
shipped inside the slim Docker image's `app/` package). Bump both when
cutting a release — see CHANGELOG.md for the version history.
"""
__version__ = "0.7.13"
