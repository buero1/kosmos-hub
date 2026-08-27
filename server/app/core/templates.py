from starlette.templating import Jinja2Templates

from app.core.timezones import format_berlin_time


def create_templates(*, directory: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=directory)
    templates.env.filters["berlin_time"] = format_berlin_time
    return templates
