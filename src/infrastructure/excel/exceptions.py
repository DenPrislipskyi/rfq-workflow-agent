class WorkbookError(Exception):
    """Base error for reading or filling an Excel workbook."""


class TemplateShapeError(WorkbookError):
    """The master workbook is not the shape this writer was built against.

    Raised rather than worked around: every one of these means a cell would go
    somewhere other than where it was meant to, and a workbook filled in the
    wrong places is worse than no workbook at all.
    """
