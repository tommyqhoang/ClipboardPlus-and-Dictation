# Overrides _pyinstaller_hooks_contrib's stock hook-workflow.py, which is written
# for the unrelated PyPI package "workflow" and calls copy_metadata("workflow") —
# a call this app's own lib/workflow.py (a same-named, unrelated local module) has
# no installed package metadata for, which crashes the build with
# ImportErrorWhenRunningHook. This hookspath entry is found first, so PyInstaller
# never loads the contrib one for this app's build.
