"""Exception types raised by wheelbarrow."""


class WheelbarrowError(Exception):
    """Base class for every error wheelbarrow reports to the user."""


class InspectionError(WheelbarrowError):
    """The input binary could not be read or its platform could not be determined."""


class MetadataError(WheelbarrowError):
    """The supplied package metadata is invalid."""


class BuildError(WheelbarrowError):
    """The build backend failed to produce a wheel."""


class PublishError(WheelbarrowError):
    """Publishing the built distributions failed."""
