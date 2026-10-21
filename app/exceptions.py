"""Domain exceptions, translated to HTTP responses in `app.main`."""


class NotFoundError(Exception):
    """A requested entity (airport, airline, country...) does not exist in the graph."""


class GdsUnavailableError(Exception):
    """The Graph Data Science plugin is missing or a projection could not be built."""


class ModelNotTrainedError(Exception):
    """The link-prediction model file has not been generated yet (`make train`)."""
