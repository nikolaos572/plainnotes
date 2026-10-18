"""Search and tag routes."""

from fastapi import APIRouter

from app import models, notes, search
from app.routes import PROTECTED, DataDirDep

router = APIRouter(dependencies=PROTECTED)


@router.get("/search")
def search_notes(
    data_dir: DataDirDep, q: str = "", sort: str | None = None, order: str | None = None
) -> list[models.SearchResult]:
    """Notes matching q, sorted, with highlighted titles and snippets."""
    notes.import_loose_files(data_dir)
    return [models.search_result(r) for r in search.search(data_dir, q, sort, order)]


@router.get("/tags")
def list_tags(data_dir: DataDirDep) -> list[models.TagCount]:
    """Every tag with the number of notes using it."""
    notes.import_loose_files(data_dir)
    return [models.TagCount(tag=tag, count=count) for tag, count in search.list_tags(data_dir)]
