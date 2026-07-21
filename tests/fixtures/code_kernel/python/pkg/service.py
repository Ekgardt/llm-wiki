from .api import PublicApi
from .base import BaseService


class Service(BaseService):
    def __init__(self, api: PublicApi) -> None:
        self.api = api

    def execute(self, value: str) -> str:
        return self.api.format(value)


def format_value(value: str) -> str:
    api = PublicApi()
    return Service(api).execute(value)
