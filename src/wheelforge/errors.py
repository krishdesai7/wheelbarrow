"""Exception types raised by wheelforge."""


class WheelforgeError(Exception):
    """Base class for every error wheelforge reports to the user."""


class InspectionError(WheelforgeError):
    """The input binary could not be read or its platform could not be determined."""


class MetadataError(WheelforgeError):
    """The supplied package metadata is invalid."""


class BuildError(WheelforgeError):
    """The build backend failed to produce a wheel."""


class FetchError(WheelforgeError):
    """A release asset could not be found, downloaded, verified or extracted."""


class PublishError(WheelforgeError):
    """Publishing the built distributions failed."""
