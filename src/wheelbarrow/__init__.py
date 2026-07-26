from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("wheelbarrow")
except PackageNotFoundError:
    __version__ = "0.1.0"  # Fallback for uninstalled local dev
