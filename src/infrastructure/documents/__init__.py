"""Attachments in, readable content out. No model is called anywhere in here."""

from src.infrastructure.documents.budget import Budget
from src.infrastructure.documents.detect import sniff
from src.infrastructure.documents.loader import DocumentLoader, SourceFile
from src.infrastructure.documents.models import (
    Document,
    FileKind,
    Grid,
    ImageRef,
    Page,
)

__all__ = [
    "Budget",
    "Document",
    "DocumentLoader",
    "FileKind",
    "Grid",
    "ImageRef",
    "Page",
    "SourceFile",
    "sniff",
]
